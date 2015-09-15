import os
import math
import json
import shelve
import warnings
import datetime
import numpy as np
from urllib import request, parse
from urllib.error import HTTPError, URLError
from orangecontrib.text.corpus import Corpus

from Orange.canvas.utils import environ
from Orange.data import Domain, StringVariable, DiscreteVariable

NYT_TEXT_FIELDS = ["headline", "lead_paragraph", "snippet", "abstract", "keywords"]


def _parse_record_json(records, includes_metadata):
    """
    Parses the JSON representation of the record returned by the New York Times Article API.
    :param records: A list of the query's results.
    :type records: list
    :param includes_metadata: The flags that determine which fields to include.
    :type includes_metadata: list
    :return: A list of the corresponding metadata and class values for the instances.
    """
    class_values = []
    metadata = np.empty((0, len(includes_metadata)+2), dtype=object)    # columns: Text fields + (pub_date + country)
    for doc in records:
        metas_row = []
        for field in includes_metadata:
            if field in doc:
                field_value = ""
                if isinstance(doc[field], dict):
                    field_value = " ".join([val for val in doc[field].values() if val])
                elif isinstance(doc[field], list):
                    field_value = " ".join([kw["value"] for kw in doc[field] if kw])
                else:
                    if doc[field]:
                        field_value = doc[field]
                metas_row.append(field_value)
        # Add the pub_date.
        metas_row.append(doc.get("pub_date", ""))
        # Add the glocation.
        metas_row.append(",".join([kw["value"] for kw in doc["keywords"] if kw["name"] == "glocations"]))

        # Add the section_name.
        class_values.append(doc.get("section_name", None))

        metas_row = [None if v is "" else v for v in metas_row]
        metadata = np.vstack((metadata, np.array(metas_row)))

    return metadata, class_values


def _date_to_str(input_date):
    """
    Returns a string representation of the input date, according to the ISO 8601 format.
    :param input_date:
    :type input_date: datetime
    :return: str
    """
    iso = input_date.isoformat()
    date_part = iso.strip().split("T")[0].split("-")
    return "%s%s%s" % (date_part[0], date_part[1], date_part[2])


def _generate_corpus(records, required_text_fields):
    """
    Generates a corpus from the input NYT records.
    :param records: The input NYT records.
    :type records: list
    :param required_text_fields: A list of the available NYT text fields.
    :type required_text_fields: list
    :return: :class: `orangecontrib.text.corpus.Corpus`
    """
    metas, class_values = _parse_record_json(records, required_text_fields)
    documents = []
    for doc in metas:
        documents.append(" ".join([d for d in doc if d is not None]).strip())

    # Create domain.
    meta_vars = [StringVariable.make(field) for field in required_text_fields]
    meta_vars += [StringVariable.make("pub_date"), StringVariable.make("country")]
    class_vars = [DiscreteVariable("section_name", values=list(set(class_values)))]
    domain = Domain([], class_vars=class_vars, metas=meta_vars)

    Y = np.array([class_vars[0].to_val(cv) for cv in class_values])[:, None]

    return Corpus(documents, None, Y, metas, domain)


class NYT:
    """
    An Orange text mining extension class for fetching records from the NYT API.
    """
    _base_url = 'http://api.nytimes.com/svc/search/v2/articlesearch.json'

    def __init__(self, api_key):
        # For accessing the API.
        self._api_key = api_key.strip()
        # API endpoint.
        self._query_url = None
        # For caching purposes.
        self.query_key = None
        # Record fields to include.
        self.includes_fields = None

        self.cache_path = None
        cache_folder = os.path.join(environ.buffer_dir, "nytcache")
        try:
            if not os.path.exists(cache_folder):
                os.makedirs(cache_folder)
            self.cache_path = os.path.join(cache_folder, "query_cache")
        except:
            warnings.warn('Could not assemble NYT query cache path', RuntimeWarning)

    def check_api_key(self):
        """
        Checks whether the api key provided to this class instance, is valid.
        :return: True or False depending on the validation outcome.
        """
        query_url = self._encode_base_url("test")
        try:
            with request.urlopen("{0}?{1}&page=0".format(self._base_url, query_url)) as connection:
                if connection.getcode() == 200:
                    return True
        except HTTPError:
            return False

    def run_query(self, query, year_from=None, year_to=None, max_records=10):
        """
        Executes the NYT query specified by the input parameters and returns a
        list of records.
        :param query: The query keywords in a string, but separated with whitespaces.
        :type query: str
        :param year_from: A digit input that signifies to return articles
            from this year forth only.
        :type year_from: int
        :param year_to: A digit input that signifies to return articles
            up to this year only.
        :type year_to: int
        :param max_records: Specifies an upper limit to the number of retrieved records.
            Max 1000.
        :type max_records: int
        :return: list
        """
        # Check API key validity first.
        if not self.check_api_key():
            warnings.warn("Cannot execute query. The specified API key is not valid.", RuntimeWarning)

        # Collect the user inputs and assemble the API endpoint.
        self._set_endpoint_url(query, year_from, year_to, NYT_TEXT_FIELDS)

        if max_records > 1000:
            warnings.warn("Cannot retrieve more than 1000 records for a particular query.", RuntimeWarning)

        num_steps = min(math.ceil(max_records/10), 100)
        records = []
        for i in range(0, num_steps):
            data, cached, err = self._execute_query(i)
            failure = (data is None) or ('response' not in data) or ('docs' not in data['response'])
            if failure:
                warnings.warn("Warning: could not retrieve page {} of specified query.".format(i))
                continue
            records.extend(data["response"]["docs"])
            # Trim the records list.
        if len(records) > max_records:
            records = records[:(max_records-len(records))]

        return _generate_corpus(records, NYT_TEXT_FIELDS)

    def _set_endpoint_url(self, query, year_from=None, year_to=None, text_includes=None):
        """
        Builds a NYT article API query url with the input parameters.
        For more information on the inputs, refer to the docs for 'run_query' method.
        :return: None
        """
        # Query keywords, base url and API key.
        query_url = self._encode_base_url(query)
        query_key = []  # This query's key, to store with shelve.

        # Use these as parts of cache keys to prevent re-use of old cache.
        query_key_sdate = _date_to_str(datetime.date(1851, 1, 1))
        query_key_edate = _date_to_str(datetime.datetime.now().date())
        # Check user date range input.
        if year_from and year_from.isdigit():
            query_key_sdate = year_from + "0101"
            query_url += "&begin_date=" + query_key_sdate
        if year_to and year_to.isdigit():
            query_key_edate = year_to + "1231"
            query_url += "&end_date=" + query_key_edate
        # Update cache keys.
        query_key.append(query_key_sdate)
        query_key.append(query_key_edate)

        # Text fields.
        if text_includes:
            self.includes_fields = text_includes
            fl = ",".join(text_includes)
            query_url += "&fl=" + fl
        # Add pub_date.
        query_url += ",pub_date"
        # Add section_name.
        query_url += ",section_name"
        # Add keywords in every case, since we need them for geolocating.
        if "keywords" not in text_includes:
            query_url += ",keywords"

        self._query_url = "{0}?{1}".format(self._base_url, query_url)

        # Queries differ in query words, included text fields and date range.
        query_key.extend(query.split(" "))
        query_key.extend(text_includes)

        self.query_key = "_".join(query_key)

    def _execute_query(self, page):
        """
        Execute a query and get the data from the New York Times Article API.
        Will not execute, if the class object's query_url has not been set.
        :param page: Determine what page of the query to return.
        :type page: int
        :return: A JSON representation of the query's results, a boolean flag,
            that serves as feedback, whether the request was cached or not and
            the error if one occurred during execution.
        """
        if not self._query_url:
            warnings.warn('Could not find any specified queries.', RuntimeWarning)

        # Method return values.
        response_data = ""
        is_cached = False

        current_query = self._query_url+"&page={}".format(page)
        current_query_key = self.query_key + "_" + str(page)

        with shelve.open(self.cache_path) as query_cache:
            if current_query_key in query_cache.keys():
                response = query_cache[current_query_key]
                response_data = json.loads(response)
                is_cached = True
                error = None
            else:
                try:
                    connection = request.urlopen(current_query)
                    response = connection.readall().decode("utf-8")
                    query_cache[current_query_key] = response

                    response_data = json.loads(response)
                    is_cached = False
                    error = None
                except (HTTPError, URLError) as err:
                    error = err

            query_cache.close()    # Release resources.
        return response_data, is_cached, error

    def _encode_base_url(self, query):
        """
        Builds the foundation url string for this query.
        :param query: The keywords for this query.
        :type query: str
        :return: The foundation url for this query.
        """
        return parse.urlencode([("q", query), ("fq", "The New York Times"), ("api-key", self._api_key)])
