import datetime
from io import StringIO
import time
from urllib.parse import urlencode
import warnings

import numpy as np
from pandas import DataFrame, concat, read_csv
import requests

from pandas_datareader._utils import (
    RemoteDataError,
    SymbolWarning,
    _init_session,
    _sanitize_dates,
)


class _BaseReader:
    """
    Parameters
    ----------
    symbols : {str, List[str]}
        String symbol of like of symbols
    start : string, int, date, datetime, Timestamp
        Starting date. Parses many different kind of date
        representations (e.g., 'JAN-01-2010', '1/1/10', 'Jan, 1, 1980')
    end : string, int, date, datetime, Timestamp
        Ending date
    retry_count : int, default 3
        Number of times to retry query request.
    pause : float, default 0.1
        Time, in seconds, of the pause between retries.
    session : Session, default None
        requests.sessions.Session instance to be used.
    freq : {str, None}
        Frequency to use in select readers
    """

    _chunk_size = 1024 * 1024
    _format = "string"

    def __init__(
        self,
        symbols,
        start=None,
        end=None,
        retry_count=3,
        pause=0.1,
        timeout=30,
        session=None,
        freq=None,
    ):
        self.symbols = symbols

        start, end = _sanitize_dates(start or self.default_start_date, end)
        self.start = start
        self.end = end

        if not isinstance(retry_count, int) or retry_count < 0:
            raise ValueError("'retry_count' must be integer larger than 0")
        self.retry_count = retry_count
        self.pause = pause
        self.timeout = timeout
        self.pause_multiplier = 1
        self.session = _init_session(session)
        self.freq = freq
        self.headers = None

    def close(self):
        """Close network session"""
        self.session.close()

    @property
    def default_start_date(self):
        """Default start date for reader. Defaults to 5 years before current date"""
        today = datetime.date.today()
        return today - datetime.timedelta(days=365 * 5)

    @property
    def url(self):
        """API URL"""
        # must be overridden in subclass
        raise NotImplementedError

    @property
    def params(self):
        """Parameters to use in API calls"""
        return None

    def read(self):
        """Read data from connector"""
        try:
            return self._read_one_data(self.url, self.params)
        finally:
            self.close()

    def _read_one_data(self, url, params):
        """read one data from specified URL"""
        if self._format == "string":
            out = self._read_url_as_StringIO(url, params=params)
        elif self._format == "json":
            out = self._get_response(url, params=params).json()
        else:
            raise NotImplementedError(self._format)
        return self._read_lines(out)

    def _read_url_as_StringIO(self, url, params=None):
        """
        Open url (and retry)
        """
        response = self._get_response(url, params=params)
        text = self._sanitize_response(response)
        out = StringIO()
        if len(text) == 0:
            service = self.__class__.__name__
            raise OSError(
                "{} request returned no data; check URL for invalid inputs: {}".format(
                    service, self.url
                )
            )
        if isinstance(text, bytes):
            out.write(text.decode("utf-8"))
        else:
            out.write(text)
        out.seek(0)
        return out

    @staticmethod
    def _sanitize_response(response):
        """
        Hook to allow subclasses to clean up response data
        """
        return response.content

    def _get_response(self, url, params=None, headers=None):
        """send raw HTTP request to get requests.Response from the specified url
        Parameters
        ----------
        url : str
            target URL
        params : dict or None
            parameters passed to the URL
        """
        headers = headers or self.headers
        pause = self.pause
        last_response_text = ""
        for _ in range(self.retry_count + 1):
            response = self.session.get(
                url, params=params, headers=headers, timeout=self.timeout
            )
            if response.status_code == requests.codes.ok:
                return response

            if response.encoding:
                last_response_text = response.text.encode(response.encoding)
            time.sleep(pause)

            # Increase time between subsequent requests, per subclass.
            pause *= self.pause_multiplier
            # Get a new breadcrumb if necessary, in case ours is invalidated
            if isinstance(params, list) and "crumb" in params:
                params["crumb"] = self._get_crumb(self.retry_count)

            # If our output error function returns True, exit the loop.
            if self._output_error(response):
                break

        if params is not None and len(params) > 0:
            url = url + "?" + urlencode(params)
        msg = f"Unable to read URL: {url}"
        if last_response_text:
            msg += f"\nResponse Text:\n{last_response_text}"

        raise RemoteDataError(msg)

    def _get_crumb(self, *args):
        """To be implemented by subclass"""
        raise NotImplementedError("Subclass has not implemented method.")

    def _output_error(self, out):
        """If necessary, a service can implement an interpreter for any non-200
         HTTP responses.

        Parameters
        ----------
        out: bytes
            The raw output from an HTTP request

        Returns
        -------
        boolean
        """
        return False

    def _read_lines(self, out):
        rs = read_csv(out, index_col=0, parse_dates=True, na_values=("-", "null"))[::-1]
        # Needed to remove blank space character in header names
        rs.columns = list(map(lambda x: x.strip(), rs.columns.values.tolist()))

        # Yahoo! Finance sometimes does this awesome thing where they
        # return 2 rows for the most recent business day
        if len(rs) > 2 and rs.index[-1] == rs.index[-2]:  # pragma: no cover
            rs = rs[:-1]
        # Get rid of unicode characters in index name.
        try:
            rs.index.name = rs.index.name.decode("unicode_escape").encode(
                "ascii", "ignore"
            )
        except AttributeError:
            # Python 3 string has no decode method.
            rs.index.name = rs.index.name.encode("ascii", "ignore").decode()

        return rs


class _DailyBaseReader(_BaseReader):
    """Base class for Google / Yahoo daily reader"""

    def __init__(
        self,
        symbols=None,
        start=None,
        end=None,
        retry_count=3,
        pause=0.1,
        session=None,
        chunksize=25,
    ):
        super().__init__(
            symbols=symbols,
            start=start,
            end=end,
            retry_count=retry_count,
            pause=pause,
            session=session,
        )
        self.chunksize = chunksize

    def _get_params(self, *args, **kwargs):
        raise NotImplementedError

    def read(self):
        """Read data"""
        # If a single symbol, (e.g., 'GOOG')
        if isinstance(self.symbols, (str, int)):
            df = self._read_one_data(self.url, params=self._get_params(self.symbols))
        # Or multiple symbols, (e.g., ['GOOG', 'AAPL', 'MSFT'])
        elif isinstance(self.symbols, DataFrame):
            df = self._dl_mult_symbols(self.symbols.index)
        else:
            df = self._dl_mult_symbols(self.symbols)
        return df

    def _dl_mult_symbols(self, symbols):
        stocks = {}
        failed = []
        passed = []
        for sym_group in _in_chunks(symbols, self.chunksize):
            for sym in sym_group:
                try:
                    stocks[sym] = self._read_one_data(self.url, self._get_params(sym))
                    passed.append(sym)
                except (OSError, KeyError):
                    msg = "Failed to read symbol: {0!r}, replacing with NaN."
                    warnings.warn(msg.format(sym), SymbolWarning, stacklevel=2)
                    failed.append(sym)

        if len(passed) == 0:
            msg = "No data fetched using {0!r}"
            raise RemoteDataError(msg.format(self.__class__.__name__))
        try:
            if len(stocks) > 0 and len(failed) > 0 and len(passed) > 0:
                df_na = stocks[passed[0]].copy()
                df_na[:] = np.nan
                for sym in failed:
                    stocks[sym] = df_na
                result = concat(stocks, sort=True).unstack(level=0)
                result.columns.names = ["Attributes", "Symbols"]
            return result
        except AttributeError as exc:
            # cannot construct a panel with just 1D nans indicating no data
            msg = "No data fetched using {0!r}"
            raise RemoteDataError(msg.format(self.__class__.__name__)) from exc


def _in_chunks(seq, size):
    """
    Return sequence in 'chunks' of size defined by size
    """
    return (seq[pos : pos + size] for pos in range(0, len(seq), size))


class _OptionBaseReader(_BaseReader):
    def __init__(self, symbol, session=None):
        """Instantiates options_data with a ticker saved as symbol"""
        self.symbol = symbol.upper()
        super().__init__(symbols=symbol, session=session)

    def get_options_data(self, month=None, year=None, expiry=None):
        """
        ***Experimental***
        Gets call/put data for the stock with the expiration data in the
        given month and year
        """
        raise NotImplementedError

    def get_call_data(self, month=None, year=None, expiry=None):
        """
        ***Experimental***
        Gets call/put data for the stock with the expiration data in the
        given month and year
        """
        raise NotImplementedError

    def get_put_data(self, month=None, year=None, expiry=None):
        """
        ***Experimental***
        Gets put data for the stock with the expiration data in the
        given month and year
        """
        raise NotImplementedError

    def get_near_stock_price(
        self, above_below=2, call=True, put=False, month=None, year=None, expiry=None
    ):
        """
        ***Experimental***
        Returns a data frame of options that are near the current stock price.
        """
        raise NotImplementedError

    def get_forward_data(
        self, months, call=True, put=False, near=False, above_below=2
    ):  # pragma: no cover
        """
        ***Experimental***
        Gets either call, put, or both data for months starting in the current
        month and going out in the future a specified amount of time.
        """
        raise NotImplementedError

    def get_all_data(self, call=True, put=True):
        """
        ***Experimental***
        Gets either call, put, or both data for all available months starting
        in the current month.
        """
        raise NotImplementedError
