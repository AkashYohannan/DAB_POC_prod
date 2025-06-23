"""
===================================================================
version         Description            Author               Date
-------------------------------------------------------------------
.10             Initial Draft         Ashwini Ramu        02/27/2025
.11             Moved common func     Ashwini Ramu        03/27/2025
.12             Added Outbound        Ashwini Ramu        04/17/2025
===================================================================
"""
import math
import requests
from requests.auth import HTTPBasicAuth
from Framework.Libraries.Utils import load_and_resolve_config, dicts_to_xml, merge_config, frame_audit_dict, update_audit_log, replace_placeholders_date, extract_json_req_field, extract_xml_req_field, xml_string_to_json, chunked, generate_hash, get_existing_table_schema
from Framework.Audit.Audit import Audit
from pyspark.sql.functions import struct, lit, current_timestamp, current_user
from pyspark.sql.types import StructType
from Framework.Libraries.DynamicSchemaGenerator import DynamicSchemaGenerator

class APIUtils:
    """
    APIUtils manages inbound and outbound data workflows involving external APIs and Delta tables.

    This class supports both API-to-Delta (Inbound) and Delta-to-API (Outbound) workflows, handling:
    - API requests with optional pagination
    - Token refresh logic
    - Audit logging
    - Data transformation and storage in Spark DataFrames

    It also handles secret resolution, placeholder replacement (e.g., dates), and schema management.

    :param spark: (SparkSession) Active Spark session
    :param log_obj: (LoggerModule) Logger object used for structured logging
    :param config_all: (dict) Configuration dictionary for the job
    """
    def __init__(self, spark, log_obj, config_all):
        """
        Initialize the APIUtils object by loading and resolving configuration, setting up audit logging, and preparing runtime variables.

        This method:
        - Merges initial config and replaces secret placeholders using Databricks secrets
        - Initializes an Audit object and inserts or retrieves an audit record
        - Updates configuration with derived parameters like `start_date` and checkpoint
        - Initializes counters for source/target tracking
        """
        self.spark = spark
        self.log_obj = log_obj
        initial_config = merge_config(config_all)
        print(initial_config)
        # creating the dynamic schema generator object
        self.dynamic_schema_object = DynamicSchemaGenerator()
        # Replace Secrets from dbutils in Config
        secret_config = load_and_resolve_config(initial_config, "aws-secrets")
        self.log_obj.log_setup(src_system_id=secret_config['source-id'], dest_system_id=secret_config['destination-id'])
        self.log_obj.log(log_level='INFO', log_msg="Replaced Secrets in Config")
        print(secret_config)

        # Get latest audit dict - Without considering Success/Failure Record
        audit_dict = frame_audit_dict(secret_config)
        self.audit_obj = Audit(self.spark, self.log_obj)
        self.audit_rec = self.audit_obj.insert_or_get_audit_record(**audit_dict)
        self.audit_id = self.audit_rec["audit_id"]
        self.processed_obj_count = self.audit_rec["loaded_obj_count"]

        # Update last_check_point to self.config_detail
        replacements = {'start_date': secret_config['adhoc_start_date'] if secret_config[
            'adhoc_start_date'] else self.audit_rec.get(
            "last_checkpoint")}
        # Derive end_date here, if needed
        # Replace the dates
        self.config = replace_placeholders_date(secret_config, replacements)
        self.log_obj.log(log_level='INFO', log_msg="Replaced last_Check_points in Config")

        self.source_count = 0
        self.target_count = 0
        self.loaded_obj_count = 0
        self.total_obj_count = 0
        self.append_df_size = 0
        self.append_df = None

    def get_new_access_token(self):
        """
        Retrieves a new access token using the refresh token.
        This method sends a POST request to the configured token endpoint to obtain a new access token.
        If the request is successful (status code 200), it updates the instance's access token and
        the 'access_token' header in the configuration. If the request fails, it logs the error and raises
        an exception.
        Args:
            None
        Returns:
            None
        Raises:
            Exception: If the request to refresh the access token fails or if the response status code is not 200.
        """
        try:
            self.log_obj.log(log_level='INFO', log_msg=f"Requesting new access token using refresh token")
            response = requests.post(self.config['url'], data=self.config['data'])
            if response.status_code == 200:
                self.access_token = response.json().get("access_token")
                self.config['headers']['access_token'] = self.access_token
                self.log_obj.log(log_level='DEBUG', log_msg=f"Access token refreshed successfully")
            else:
                self.log_obj.log(log_level='ERROR', log_msg=f"Failed to refresh access token: {response.text}")
                raise Exception("Failed to refresh access token.")
        except requests.RequestException as err:
            self.log_obj.log(log_level='ERROR', log_msg=f"Error during token refresh: {err}")
            raise Exception("Token refresh failed.")

    def api_request(self, method=None, url=None, username=None, password=None, headers=None, params=None, data=None, body=None):
        """
        Sends an HTTP request to a specified endpoint using the provided parameters or defaults from the configuration.
        This method determines the request parameters by prioritizing the explicitly provided arguments.
        If any argument is not provided, it falls back to the corresponding value in the instance's configuration.
        Args:
            method (str, optional): The HTTP method to use for the request (e.g., 'GET', 'POST').
                Defaults to the 'method' specified in the instance's configuration.
            url (str, optional): The URL to which the request is sent.
                Defaults to the 'url' specified in the instance's configuration.
            auth (tuple or requests.auth.AuthBase, optional): Authentication credentials.
                Defaults to the 'auth' specified in the instance's configuration.
            headers (dict, optional): HTTP headers to include in the request.
                Defaults to the 'headers' specified in the instance's configuration.
            params (dict, optional): URL parameters to append to the URL.
                Defaults to the 'params' specified in the instance's configuration.
            data (dict or bytes, optional): Data to send in the body of the request (for form-encoded data).
                Defaults to the 'data' specified in the instance's configuration.
            body (dict, optional): JSON data to send in the body of the request.
                Defaults to the 'body' specified in the instance's configuration.
        Returns:
            Response: The response object returned by the `handle_response` method.
        Raises:
            requests.RequestException: If an error occurs during the HTTP request.
        """
        req_method = method if method else self.config['method']
        req_url = url if url else self.config['url']
        req_headers= headers if headers else self.config['headers']
        req_params = params if params else self.config.get('params')
        req_data = data if data else self.config.get('data')
        req_body = body if body else self.config.get('body')
        req_username = username if username else self.config.get('username')
        req_password = password if password else self.config.get('password')
        if self.config.get('auth-type') == "HTTPBasicAuth":
            req_auth = HTTPBasicAuth(req_username,req_password)
        else:
            req_auth = None
        try:
            self.log_obj.log(log_level='INFO', log_msg=f"{req_method} request to {req_url} with params: {req_params}")
            print(req_method, req_url, req_headers, req_params, req_data, req_body, req_username, req_password)
            response = requests.request(method=req_method, url=req_url, auth=req_auth, headers=req_headers, params=req_params, data=req_data, json=req_body)
            print(f"[DEBUG] Received status code: {response.status_code}")
            return self.handle_response(response, req_params)
        except requests.RequestException as err:
            self.log_obj.log(log_level='ERROR', log_msg=f"Error during {req_method} request: {err}")
            raise err

    def handle_response(self, response: requests.Response, final_params):
        """
        Processes the HTTP response by evaluating its status code and taking appropriate actions.
        This method performs the following steps:
        1. Checks if the response status code is 200 (OK):
           - Logs a success message.
           - Returns the response content as a string.
        2. Checks if the response status code is 401 (Unauthorized):
           - Logs a warning about potential access token expiration.
           - Attempts to refresh the access token by calling the `get_new_access_token` method.
           - Recursively retries the API request with the updated access token.
        3. For other status codes:
           - Logs an error message indicating the failure.
           - Raises an exception to signal the failure.
        Args:
            response (requests.Response): The HTTP response object to process.
            final_params (dict): The parameters to use when retrying the API request after refreshing the access token.
        Returns:
            str: The content of the response if the status code is 200.
        Raises:
            Exception: If the response status code is not 200 or 401, or if token refresh fails.
        """
        if response.status_code == 200:
            self.log_obj.log(log_level='INFO', log_msg=f"API Response is successful, status code {response.status_code}")
            print(f"Response :\n{response.text}")
            return response.text
        elif response.status_code == 401:  # Unauthorized - Access Token may have expired
            self.log_obj.log(log_level='WARNING', log_msg="Access token expired. Refreshing token..")
            self.get_new_access_token()
            # Retry the request with the new access token
            self.api_request(params=final_params)  # Recursively retry the GET request
        else:
            self.log_obj.log(log_level='ERROR', log_msg=f"Request failed with status code {response.status_code}")
            raise Exception("Request failed with status code")

    # def fetch_data_api(self, params=None):
    #     """
    #     Fetches data from an API and returns a Spark DataFrame along with the source count.
    #
    #     This method makes an API request using the `api_request` method with the provided parameters
    #     and processes the response based on the specified format (JSON or XML). The response is parsed
    #     accordingly, and a Spark DataFrame is created from the extracted data. It also extracts the source
    #     count based on configuration and handles both JSON and XML response formats.
    #     Steps:
    #         1. Makes the API request using the provided parameters.
    #         2. If the response format is 'json':
    #             - Extracts the source count if the 'count-field' is provided in the configuration.
    #             - Extracts the API data from the 'data-field' and creates a Spark DataFrame.
    #         3. If the response format is 'xml':
    #             - Extracts and processes XML data, converting it to JSON if necessary and creating a Spark DataFrame.
    #             - Sets the source count and total count.
    #         4. If the response format is neither 'json' nor 'xml', creates an empty Spark DataFrame.
    #     Args:
    #         params (dict, optional): Parameters to pass in the API request. Defaults to None.
    #     Returns:
    #         tuple: A tuple containing:
    #             - api_df (DataFrame): The Spark DataFrame containing the API response data.
    #             - source_count (int): The count of the source data items extracted from the API response.
    #     Raises:
    #         Exception: If the API response format is unsupported or if there is an issue processing the data.
    #     """
    #     api_response = self.api_request(params=params)
    #     if self.config['response-format'] == 'json':
    #         if self.config.get('count-field'):
    #             self.source_count = extract_json_req_field(api_response, self.config['count-field'])
    #             #print(f"total count :{self.source_count}")
    #         if self.config.get('data-field'):
    #             api_data = extract_json_req_field(api_response,self.config['data-field'])
    #             if api_data:
    #                 cleaned_data = [{k: str(v) for k, v in row.items()} for row in api_data]
    #                 api_df = self.spark.createDataFrame(cleaned_data)
    #             else:
    #                 api_df = self.spark.createDataFrame([], StructType([]))
    #     elif self.config['response-format'] == 'xml':
    #         if self.config.get('data-field'):
    #             if "wsdl" in self.config['url']:
    #                 api_data = extract_xml_req_field(api_response, self.config['data-field'], self.config['namespace'])
    #             else:
    #                 api_dict = xml_string_to_json(api_response)
    #                 api_data = extract_json_req_field(api_dict, self.config['data-field'])
    #             api_df = self.spark.createDataFrame(api_data)
    #             self.source_count = len(api_data)
    #             self.total_obj_count = 1
    #             self.processed_obj_count = 1
    #     else:
    #         api_df = self.spark.createDataFrame(api_response, StructType([]))
    #
    #     return api_df, self.source_count



    def fetch_data_api(self, params=None):
        """
        Fetches data from an external API endpoint and returns it as a Spark DataFrame.

        Supports both JSON and XML response formats, handles custom data extraction fields, and infers source count if configured.

        :param params: (dict) Query parameters to use for the request (optional)
        :return: (tuple) A tuple (api_df, source_count) where:
                 - api_df (DataFrame): Spark DataFrame created from API response
                 - source_count (int): Count of records retrieved from the source
        """
        #  Step 1: Make the API request
        api_response = self.api_request(params=params)

        #  Step 2: Configuration shortcuts
        response_format = self.config.get('response-format', 'json')
        data_field = self.config.get('data-field')
        count_field = self.config.get('count-field')

        #  Step 3: Initialize default values
        api_data = []
        api_df = self.spark.createDataFrame([], StructType([]))  # Empty fallback DataFrame

        #  Step 4: Handle JSON API response
        if response_format == 'json':
            # Extract source count from the API response if defined
            if count_field:
                self.source_count = extract_json_req_field(api_response, count_field)

            # Extract data records using the configured data field
            if data_field:
                api_data = extract_json_req_field(api_response, data_field)

        #  Step 5: Handle XML API response
        elif response_format == 'xml' and data_field:
            if "wsdl" in self.config.get('url', ''):
                # For SOAP-based WSDL endpoints
                api_data = extract_xml_req_field(api_response, data_field, self.config['namespace'])
            else:
                # For raw XML string responses
                api_dict = xml_string_to_json(api_response)
                api_data = extract_json_req_field(api_dict, data_field)

            # Set source count metrics for XML
            if api_data:
                self.source_count = len(api_data)
                self.total_obj_count = 1
                self.processed_obj_count = 1

        #  Step 6: Common transformation logic for both JSON and XML
        # if api_data:
        #     # Create final Spark DataFrame with the new schema
        #     cleaned_data = [{k: str(v) for k, v in row.items()} for row in api_data]
        #     api_df = self.spark.createDataFrame(cleaned_data)
        #
        # # k, Step 7: Return the final DataFrame and source count
        # return api_df, self.source_count
        if api_data:
            print(f"api_data datatype : {type(api_data)}")
            # Compose fully qualified Spark table name
            full_table_name = f"{self.config['catalog-name']}.{self.config['schema-name']}.{self.config['table-name']}"
            print(full_table_name)

            # Fetch the existing schema from the catalog table
            existing_schema = get_existing_table_schema(self.spark, full_table_name, self.dynamic_schema_object, "struct")
            print(f"existing_schema:{existing_schema}")

            # Fetch ignore fields
            ignore_fields = self.dynamic_schema_object.get_ignore_fields(self.spark, full_table_name)
            print(f"ignore_fields:{ignore_fields}")

            # Generate updated schema based on API data
            current_schema, distinct_schemas, schema_diff = self.dynamic_schema_object.generate_dynamic_schema(api_data, existing_schema, None, ignore_fields)
            print(f"current_schema:{current_schema}")
            print(f"schema_diff: {schema_diff}")

            if schema_diff.get('only_in_current'):
                self.dynamic_schema_object.schema_audit(self.spark, full_table_name, existing_schema, current_schema, schema_diff, ignore_fields)

            # Convert raw API data into Spark-compatible structured records
            transformed_data = [self.dynamic_schema_object.convert_record(rec, current_schema) for rec in api_data]
            print(schema_diff)
            # Generate SQL column definitions for downstream use (e.g., DDL, merge)
            sql_column_definitions = self.dynamic_schema_object.generate_table_columns()
            print(sql_column_definitions)

            # Create final Spark DataFrame with the new schema
            api_df = self.spark.createDataFrame(transformed_data, schema=current_schema)

            # k, Step 7: Return the final DataFrame and source count
        return api_df, self.source_count

    def set_pagination_params(self, total_count): #0 or 2
        """
        Calculates and sets pagination parameters based on the total number of items and the current processing state.
        This method updates the 'offset' and 'limit' values in the configuration's parameters to fetch the appropriate
        subset of data for the current processing iteration. It ensures that the pagination does not exceed the total
        available items.
        Args:
            total_count (int): The total number of items available for processing.
        Returns:
            bool: True if the current page is the last page (i.e., no more data to process after this), False otherwise.
        """
        offset = self.config['params']['limit'] * self.processed_obj_count
        limit = self.config['params']['limit']
        if (offset + limit) > total_count:
            curr_limit = total_count - offset
            is_last_page = True
        else:
            curr_limit = limit
            is_last_page = False
        self.config['params']['offset'] = offset
        self.config['params']['limit'] = curr_limit
        return is_last_page

    def set_pagination_body(self, total_count): #0 or 2
        """
        Updates the pagination parameters in the request body based on the total number of items.
        This method increments the 'pageIndex' in the request body to fetch the next page of data.
        It calculates the total number of pages using the 'pageSize' and determines if the current
        page is the last one.
        Args:
            total_count (int): The total number of items available for pagination.
        Returns:
            bool: True if the current page is the last page; False otherwise.
        """
        self.config['body']['pageIndex'] = self.processed_obj_count + 1
        total_obj_count = math.ceil(total_count / self.config['body']['pageSize'])
        is_last_page = True if (self.config['body']['pageIndex'] == total_obj_count) else False
        return is_last_page

    def set_pagination(self, total_count):
        """
        Determines and applies the appropriate pagination strategy based on the configuration.
        This method selects the pagination approach specified in the configuration and delegates
        the task to the corresponding method to update pagination parameters accordingly.
        Args:
            total_count (int): The total number of items available for pagination.
        Returns:
            bool: True if the current page is the last page; False otherwise.
        Raises:
            ValueError: If an unsupported pagination type is specified in the configuration.
        """
        if self.config['pagination-type'] == "limit-offset":
            return self.set_pagination_params(total_count)
        elif self.config['pagination-type'] == "pageindex-size":
            return self.set_pagination_body(total_count)
        else:
            self.log_obj.log(log_level='ERROR', log_msg=f"Provide right pagination_type: {self.config['pagination-type']}")
            raise ValueError(f"Provide right pagination_type: {self.config['pagination-type']}")

    def load_data_db(self, api_df, is_last_page=None):
        """
        Loads data from a DataFrame into a Delta table, appending new records and managing pagination.

        This method performs the following steps:
        1. Appends the provided DataFrame (`api_df`) to an accumulating DataFrame (`append_df`).
        2. Monitors the accumulated DataFrame's size (`append_df_size`) against a configured limit (`df-limit`).
        3. When the accumulated size reaches the limit or when the last page of data is indicated:
           - Replaces empty strings in `api_df` with `None`.
           - Generates a unique `surrogate_key_id` for each record using a hash function.
           - Adds metadata columns: `source_id`, `created_at`, and `updated_at`.
           - Selects relevant columns, including the metadata and the original data.
           - Writes the formatted DataFrame to a Delta table using append mode.
           - Resets the accumulating DataFrame and its size tracker.
        4. Updates counters tracking the number of records processed and loaded.

        Args:
            api_df (DataFrame): The DataFrame containing data to be loaded into the database.
            is_last_page (bool, optional): Flag indicating whether the current data represents the last page. Defaults to None.

        """
        # write_mode = "overwrite" if self.load_type == "full load" else "append"
        write_mode = "append"
        df_count = api_df.count()
        self.append_df_size = self.append_df_size + df_count  # append_df_size & append_df is added to init because, this function in called in loop & it has to hold the value
        self.append_df = self.append_df.union(api_df)
        # print("Appended to dataframe")
        if self.append_df_size >= self.config['df-limit'] or is_last_page or not self.config.get('pagination-type'):
            api_df = api_df.replace('', None)
            formatted_df = (api_df.withColumn("surrogate_key_id", lit(generate_hash(self.config['source-id'],current_timestamp(),struct(*self.append_df.columns))))
                  .withColumn("source_id", lit(self.config['source-id']))
                  .withColumn("created_by", current_user())
                  .withColumn("created_timestamp", current_timestamp())
                  .withColumn("updated_by", current_user())
                  .withColumn("updated_timestamp", current_timestamp())
                  .select("surrogate_key_id", "source_id", "created_by", "created_timestamp", "updated_by", "updated_timestamp",
                          struct(*self.append_df.columns).alias("data_payload")))
            formatted_df.write.mode(write_mode).option("MergeSchema", "true").saveAsTable(f"{self.config['catalog-name']}.{self.config['schema-name']}.{self.config['table-name']}")
            self.log_obj.log(log_level='INFO', log_msg=f"Appended to delta table : {self.config['catalog-name']}.{self.config['schema-name']}.{self.config['table-name']}")
            self.target_count = self.target_count + self.append_df_size
            self.loaded_obj_count = self.processed_obj_count
            self.append_df = self.append_df.limit(0)
            self.append_df_size = 0

    def derive_cnt_params(self):
        """
        Derives count parameters based on the configured pagination type.

        This method examines the 'pagination-type' specified in the configuration and determines
        the appropriate parameters to use for counting the total number of items in a paginated dataset.

        If the pagination type is 'limit-offset', it copies the existing parameters and removes
        the 'limit' key, as it is not needed for counting the total items.

        If the pagination type is not 'limit-offset', it returns None, indicating that count parameters
        are not applicable or are handled differently.
        Returns:
            dict or None: A dictionary of parameters suitable for counting total items, or None if not applicable.
        Example:
            count_params = self.derive_cnt_params()
            if count_params:
                # Use count_params to fetch the total count of items
                pass
            else:
                # Handle cases where count parameters are not used
                pass
        """
        if self.config['pagination-type'] == "limit-offset":
            # print(self.params)
            cnt_params = self.config.get('params').copy()
            del cnt_params["limit"]
        else:
            cnt_params = None
        return cnt_params

    def fetch_data_db(self):
        """
        Fetches data from a Delta table using a SQL query defined in the configuration.

        Returns data as a list of dictionaries and updates `source_count`.

        :return: (tuple) A tuple (query_data, source_count) where:
                 - query_data (list[dict]): Rows from the Delta table
                 - source_count (int): Number of rows fetched
        """
        query_df = self.spark.sql(f"{self.config['source-query']}")
        query_data = [row.asDict() for row in query_df.collect()]
        print(query_data)
        self.source_count = len(query_data)
        self.log_obj.log(log_level='INFO', log_msg=f"fetched data from db:{self.source_count}")
        return query_data, self.source_count

    def load_data_api(self, data_list, source_count):
        """
        Pushes data from Delta to an API endpoint in chunks.

        Supports multiple API data formats:
        - array_dict (batched JSON objects)
        - dict (single JSON per request)
        - xml (future support)
        - nested_json (future support)

        :param data_list: (list) List of JSON-serializable dictionaries
        :param source_count: (int) Total number of records to push
        """
        self.total_obj_count = math.ceil(source_count / self.config['api-limit']) if self.config.get('api-limit') else 1
        if self.config['api-dataformat'] == "array_dict":
            print(f"API Output format : {self.config['api-dataformat']}")
            start = self.processed_obj_count + 1
            chunk_size = self.config.get('api-limit') if self.config.get('api-limit') else source_count
            print(chunk_size)
            for batch_num, chunk in enumerate(chunked(data_list, chunk_size), start=start):
                self.log_obj.log(log_level='INFO', log_msg=f"Sending batch {batch_num} with {len(chunk)} records")
                self.api_request(body=json.dumps(chunk))
                self.loaded_obj_count = batch_num
                self.target_count += len(chunk)
        elif self.config['api-dataformat'] == "dict":
            print(f"API Output format : {self.config['api-dataformat']}")
            print(type(data_list))
            for json in data_list:
                self.api_request(body=json)
                self.target_count += 1
        elif self.config['api-dataformat'] == "xml":
            start = self.processed_obj_count + 1
            chunk_size = self.config.get('api-limit') if self.config.get('api-limit') else source_count
            for batch_num, chunk in enumerate(chunked(data_list, chunk_size), start=start):
                xml_output = dicts_to_xml(chunk) #provide root_name & item_name
                self.api_request(body=xml_output)
                self.loaded_obj_count = batch_num
                self.target_count += len(chunk)
        elif self.config['api-dataformat'] == "nested_json":
            print(f"API Output format : {self.config['api-dataformat']}")
            start = self.processed_obj_count + 1
            chunk_size = self.config.get('api-limit') if self.config.get('api-limit') else source_count
            print(chunk_size)
            nested_json = {}
            for batch_num, chunk in enumerate(chunked(data_list, chunk_size), start=start):
                self.log_obj.log(log_level='INFO', log_msg=f"Sending batch {batch_num} with {len(chunk)} records")
                nested_json['results'] = chunk
                self.api_request(body=nested_json)
                self.loaded_obj_count = batch_num
                self.target_count += len(chunk)
        self.log_obj.log(log_level='ERROR', log_msg=f"Pass the correct API Data format")

    def execute_inbound(self):
        """
        Executes the inbound data flow by fetching data from an API and loading it into a database.

        This method performs the following steps:
        1. Initializes pagination parameters if a pagination type is specified in the configuration.
        2. Fetches the total count of records from the API to determine the number of pages.
        3. Iteratively fetches data from the API in pages and appends it to a DataFrame.
        4. Loads the fetched data into a database table.
        5. Updates the audit log with the status 'Success' and relevant metrics upon successful completion.

        If any error occurs during the execution, the following steps are performed:
        1. Logs the error message.
        2. Updates the audit log with the status 'Failed' and relevant metrics.
        3. Raises the exception to propagate the error.

        Returns:
            str: The audit ID associated with the execution.

        Raises:
            Exception: If any error occurs during the data fetch or load operations.
        """
        try:
            is_last_page = None
            if self.config.get('pagination-type'):
                # Set params to fetch total_count
                cnt_params = self.derive_cnt_params()
                # Fetch total count from API before data fetch
                api_df, source_count = self.fetch_data_api(cnt_params) # In Reprocessing also, considering the Record account fetched in Request
                self.log_obj.log(log_level='INFO', log_msg=f"Total response count:{source_count}")
                is_last_page = self.set_pagination(source_count) # Need to check is_last_page irrespective of audit_status, In case total_count < limit the request would give error
            while True:
                # Fetch data from API
                api_df, source_count = self.fetch_data_api()
                if source_count > 0:
                    self.append_df = self.spark.createDataFrame([], api_df.schema)  # Created an empty dataframe with a schema for appending data
                    # Write data to Delta tables
                    self.load_data_db(api_df, is_last_page)
                    # Update Pagination params
                    if self.config.get('pagination-type'):
                        self.processed_obj_count += 1
                        # print(self.processed_obj_count)
                        if is_last_page:
                            break
                        is_last_page = self.set_pagination(source_count)
                    else:
                        break
                else:
                    break
            update_audit_log(self.audit_obj, self.audit_id, "Success", self.source_count, self.total_obj_count, self.loaded_obj_count, self.target_count)
            #self.update_audit_log("Success")
            self.log_obj.log(log_level='INFO', log_msg=f"Updated audit log")
            return self.audit_id
        except Exception as err:
            self.log_obj.log(log_level='ERROR', log_msg=f"Script failed due to error : {err}")
            update_audit_log(self.audit_obj, self.audit_id, "Failed", self.source_count, self.total_obj_count,
                             self.loaded_obj_count, self.target_count)
            #self.update_audit_log("Failed")
            raise err

    def execute_outbound(self):
        """
        Executes the outbound data flow by fetching data from a database and loading it to an API.
        This method performs the following steps:
        1. Fetches data from the database using the `fetch_data_db` method.
        2. Logs the successful data fetch operation.
        3. If data is fetched successfully (i.e., `query_data_list` is not empty), it loads the data to the API using the `load_data_api` method.
        4. Logs the successful data load operation.
        5. Updates the audit log with the status 'Success' and relevant metrics.

        If any error occurs during the execution, the following steps are performed:
        1. Logs the error message.
        2. Updates the audit log with the status 'Failed' and relevant metrics.
        3. Raises the exception to propagate the error.
        Returns:
            None
        Raises:
            Exception: If any error occurs during the data fetch or load operations.
        """
        try:
            query_data_list, source_count = self.fetch_data_db()
            self.log_obj.log(log_level='INFO', log_msg=f"Fetched data from databricks")
            if source_count > 0:
                self.load_data_api(query_data_list, source_count)
                self.log_obj.log(log_level='INFO', log_msg=f"Loaded data to API")
            update_audit_log(self.audit_obj, self.audit_id,"Success", self.source_count, self.total_obj_count,
                             self.loaded_obj_count, self.target_count)
        except Exception as err:
            self.log_obj.log(log_level='ERROR', log_msg=f"Script failed due to error : {err}")
            update_audit_log(self.audit_obj, self.audit_id,"Failed", self.source_count, self.total_obj_count,
                             self.loaded_obj_count, self.target_count)
            raise err

    def execute(self):
        """
        Executes the appropriate data flow operation based on the configuration.
        This method checks the 'dataflow' key in the configuration and delegates
        execution to the corresponding method:
        - If 'dataflow' is 'Inbound', it calls the `execute_inbound` method.
        - If 'dataflow' is 'Outbound', it calls the `execute_outbound` method.
        - If 'dataflow' has any other value, it logs an error and raises a ValueError.
        Returns:
            The result of the called execution method (`execute_inbound` or `execute_outbound`).
        Raises:
            ValueError: If the 'dataflow' configuration is not 'Inbound' or 'Outbound'.
        """
        if self.config['dataflow'] == 'Inbound':
            return self.execute_inbound()
        elif self.config['dataflow'] == 'Outbound':
            return self.execute_outbound()
        else:
            self.log_obj.log(log_level='ERROR', log_msg=f"Provide a valid dataflow")