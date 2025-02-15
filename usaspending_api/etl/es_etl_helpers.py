import boto3
import json
import os
import pandas as pd
import psycopg2
import subprocess
import tempfile

from collections import defaultdict
from datetime import datetime
from django.conf import settings
from django.core.management import call_command
from elasticsearch import helpers, TransportError
from time import perf_counter, sleep

from usaspending_api.awards.v2.lookups.elasticsearch_lookups import INDEX_ALIASES_TO_AWARD_TYPES
from usaspending_api.common.csv_helpers import count_rows_in_delimited_file
from usaspending_api.common.helpers.sql_helpers import get_database_dsn_string

# ==============================================================================
# SQL Template Strings for Postgres Statements
# ==============================================================================

VIEW_COLUMNS = [
    "transaction_id",
    "detached_award_proc_unique",
    "afa_generated_unique",
    "generated_unique_transaction_id",
    "display_award_id",
    "update_date",
    "modification_number",
    "generated_unique_award_id",
    "award_id",
    "piid",
    "fain",
    "uri",
    "award_description",
    "product_or_service_code",
    "product_or_service_description",
    "naics_code",
    "naics_description",
    "type_description",
    "award_category",
    "recipient_unique_id",
    "parent_recipient_unique_id",
    "recipient_hash",
    "recipient_name",
    "action_date",
    "fiscal_action_date",
    "period_of_performance_start_date",
    "period_of_performance_current_end_date",
    "ordering_period_end_date",
    "transaction_fiscal_year",
    "award_fiscal_year",
    "award_amount",
    "transaction_amount",
    "face_value_loan_guarantee",
    "original_loan_subsidy_cost",
    "generated_pragmatic_obligation",
    "awarding_agency_id",
    "funding_agency_id",
    "awarding_toptier_agency_id",
    "funding_toptier_agency_id",
    "awarding_subtier_agency_id",
    "funding_subtier_agency_id",
    "awarding_toptier_agency_name",
    "funding_toptier_agency_name",
    "awarding_subtier_agency_name",
    "funding_subtier_agency_name",
    "awarding_toptier_agency_abbreviation",
    "funding_toptier_agency_abbreviation",
    "awarding_subtier_agency_abbreviation",
    "funding_subtier_agency_abbreviation",
    "awarding_toptier_agency_code",
    "funding_toptier_agency_code",
    "awarding_subtier_agency_code",
    "funding_subtier_agency_code",
    "cfda_id",
    "cfda_number",
    "cfda_title",
    "cfda_popular_name",
    "type_of_contract_pricing",
    "type_set_aside",
    "extent_competed",
    "type",
    "pop_country_code",
    "pop_country_name",
    "pop_state_code",
    "pop_county_code",
    "pop_county_name",
    "pop_zip5",
    "pop_congressional_code",
    "pop_city_name",
    "recipient_location_country_code",
    "recipient_location_country_name",
    "recipient_location_state_code",
    "recipient_location_county_code",
    "recipient_location_county_name",
    "recipient_location_zip5",
    "recipient_location_congressional_code",
    "recipient_location_city_name",
    "treasury_accounts",
    "federal_accounts",
    "business_categories",
]

UPDATE_DATE_SQL = " AND update_date >= '{}'"

COUNT_SQL = """
SELECT COUNT(*) AS count
FROM {view}
WHERE transaction_fiscal_year={fy}{update_date}
"""

COPY_SQL = """"COPY (
    SELECT *
    FROM {view}
    WHERE transaction_fiscal_year={fy}{update_date}
) TO STDOUT DELIMITER ',' CSV HEADER" > '{filename}'
"""

CHECK_IDS_SQL = """
WITH temp_transaction_ids AS (
  SELECT *
  FROM (VALUES {id_list}) AS unique_id_list (generated_unique_transaction_id)
)
SELECT transaction_id, generated_unique_transaction_id, update_date
FROM {view}
WHERE EXISTS (
  SELECT *
  FROM temp_transaction_ids
  WHERE
    {view}.generated_unique_transaction_id = temp_transaction_ids.generated_unique_transaction_id
    AND transaction_fiscal_year={fy}
)
"""

# ==============================================================================
# Other Globals
# ==============================================================================

AWARD_DESC_CATEGORIES = {
    "loans": "loans",
    "grant": "grants",
    "insurance": "other",
    "other": "other",
    "contract": "contracts",
    "direct payment": "directpayments",
}

UNIVERSAL_TRANSACTION_ID_NAME = "generated_unique_transaction_id"


class DataJob:
    def __init__(self, *args):
        self.name = args[0]
        self.index = args[1]
        self.fy = args[2]
        self.csv = args[3]
        self.count = None


# ==============================================================================
# Helper functions for several Django management commands focused on ETL into a Elasticsearch cluster
# ==============================================================================


def convert_postgres_array_as_string_to_list(array_as_string: str) -> list:
    """
        Postgres arrays are stored in CSVs as strings. Elasticsearch is able to handle lists of items, but needs to
        be passed a list instead of a string. In the case of an empty array, return null.
        For example, "{this,is,a,postgres,array}" -> ["this", "is", "a", "postgres", "array"].
    """
    return array_as_string[1:-1].split(",") if len(array_as_string) > 2 else None


def convert_postgres_json_array_as_string_to_json(json_array_as_string: str) -> dict:
    """
        Postgres JSON arrays (jsonb) are stored in CSVs as strings. Elasticsearch is able to handle
        JSON arrays with nested types, but needs to be passed a list JSON instead of a string.
        In the case of an empty array, return null.
    """
    return json.loads(json_array_as_string) if json_array_as_string else None


def process_guarddog(process_list):
    """
        pass in a list of multiprocess Process objects.
        If one errored then terminate the others and return True
    """
    for proc in process_list:
        # If exitcode is None, process is still running. exit code 0 is normal
        if proc.exitcode not in (None, 0):
            msg = "TERMINATING ALL PROCESSES AND QUITTING!!! " + "{} exited with error. Returned {}".format(
                proc.name, proc.exitcode
            )
            printf({"msg": msg})
            [x.terminate() for x in process_list]
            return True
    return False


def configure_sql_strings(config, filename, deleted_ids):
    """
    Populates the formatted strings defined globally in this file to create the desired SQL
    """
    update_date_str = UPDATE_DATE_SQL.format(config["starting_date"].strftime("%Y-%m-%d"))

    copy_sql = COPY_SQL.format(
        fy=config["fiscal_year"],
        update_date=update_date_str,
        filename=filename,
        view=settings.ES_TRANSACTIONS_ETL_VIEW_NAME,
    )

    count_sql = COUNT_SQL.format(
        fy=config["fiscal_year"], update_date=update_date_str, view=settings.ES_TRANSACTIONS_ETL_VIEW_NAME
    )

    if deleted_ids and config["process_deletes"]:
        id_list = ",".join(["('{}')".format(x) for x in deleted_ids.keys()])
        id_sql = CHECK_IDS_SQL.format(id_list=id_list, fy=config["fiscal_year"])
    else:
        id_sql = None

    return copy_sql, id_sql, count_sql


def execute_sql_statement(cmd, results=False, verbose=False):
    """ Simple function to execute SQL using a psycopg2 connection"""
    rows = None
    if verbose:
        print(cmd)

    with psycopg2.connect(dsn=get_database_dsn_string()) as connection:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(cmd)
            if results:
                rows = db_rows_to_dict(cursor)
    return rows


def db_rows_to_dict(cursor):
    """ Return a dictionary of all row results from a database connection cursor """
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def download_db_records(fetch_jobs, done_jobs, config):
    # There has been a reoccuring issue with .empty() returning true when the queue actually
    # contains multiple jobs. Wait a few seconds before starting to see if it helps
    sleep(5)
    printf({"msg": "Queue has items: {}".format(not fetch_jobs.empty()), "f": "Download"})
    while not fetch_jobs.empty():
        if done_jobs.full():
            printf({"msg": "Paused downloading new CSVs so ES indexing can catch up", "f": "Download"})
            sleep(60)
        else:
            start = perf_counter()
            job = fetch_jobs.get_nowait()
            printf({"msg": 'Preparing to download "{}"'.format(job.csv), "job": job.name, "f": "Download"})

            sql_config = {
                "starting_date": config["starting_date"],
                "fiscal_year": job.fy,
                "process_deletes": config["process_deletes"],
            }
            copy_sql, _, count_sql = configure_sql_strings(sql_config, job.csv, [])

            if os.path.isfile(job.csv):
                os.remove(job.csv)

            job.count = download_csv(count_sql, copy_sql, job.csv, job.name, config["skip_counts"], config["verbose"])
            done_jobs.put(job)
            printf(
                {
                    "msg": 'CSV "{}" copy took {} seconds'.format(job.csv, perf_counter() - start),
                    "job": job.name,
                    "f": "Download",
                }
            )
            sleep(1)

    # This "Null Job" is used to notify the other (ES data load) process this is the final job
    done_jobs.put(DataJob(None, None, None, None))
    printf({"msg": "PostgreSQL COPY operations complete", "f": "Download"})
    return


def download_csv(count_sql, copy_sql, filename, job_id, skip_counts, verbose):
    if skip_counts:
        count = None
        printf({"msg": "Skipping count checks. Writing file: {}".format(filename), "job": job_id, "f": "Download"})
    else:
        count = execute_sql_statement(count_sql, True, verbose)[0]["count"]
        printf({"msg": "Writing {} to this file: {}".format(count, filename), "job": job_id, "f": "Download"})
    # It is preferable to not use shell=True, but this command works. Limited user-input so risk is low
    subprocess.Popen('psql "${{DATABASE_URL}}" -c {}'.format(copy_sql), shell=True).wait()

    if not skip_counts:
        download_count = count_rows_in_delimited_file(filename, has_header=True, safe=False)
        if count != download_count:
            msg = "Mismatch between CSV and DB rows! Expected: {} | Actual {} in: {}"
            printf({"msg": msg.format(count, download_count, filename), "job": job_id, "f": "Download"})
            raise SystemExit(1)
    return count


def csv_chunk_gen(filename, chunksize, job_id):
    printf({"msg": "Opening {} (batch size = {})".format(filename, chunksize), "job": job_id, "f": "ES Ingest"})
    # Need a specific converter to handle converting strings to correct data types (e.g. string -> array)
    converters = {
        "business_categories": convert_postgres_array_as_string_to_list,
        "treasury_accounts": convert_postgres_json_array_as_string_to_json,
        "federal_accounts": convert_postgres_json_array_as_string_to_json,
    }
    # Panda's data type guessing causes issues for Elasticsearch. Explicitly cast using dictionary
    dtype = {k: str for k in VIEW_COLUMNS if k not in converters}
    for file_df in pd.read_csv(filename, dtype=dtype, converters=converters, header=0, chunksize=chunksize):
        file_df = file_df.where(cond=(pd.notnull(file_df)), other=None)
        yield file_df.to_dict(orient="records")


def es_data_loader(client, fetch_jobs, done_jobs, config):
    if config["create_new_index"]:
        # ensure template for index is present and the latest version
        call_command("es_configure", "--template-only")
    while True:
        if not done_jobs.empty():
            job = done_jobs.get_nowait()
            if job.name is None:
                break

            printf({"msg": "Starting new job", "job": job.name, "f": "ES Ingest"})
            post_to_elasticsearch(client, job, config)
            if os.path.exists(job.csv):
                os.remove(job.csv)
        else:
            printf({"msg": "No Job. Sleeping 45s", "f": "ES Ingest"})
            sleep(45)

    printf({"msg": "Completed Elasticsearch data load", "f": "ES Ingest"})
    return


def streaming_post_to_es(client, chunk, index_name, job_id=None):
    success, failed = 0, 0
    try:
        # "doc_type" is set in the index templete file. Don't change this without changing in json file first
        for ok, item in helpers.streaming_bulk(client, chunk, index=index_name, doc_type="transaction_mapping"):
            success = [success, success + 1][ok]
            failed = [failed + 1, failed][ok]

    except Exception as e:
        print("Fatal error: \n\n{}...\n\n{}".format(str(e)[:5000], "*" * 80))
        raise SystemExit(1)

    printf({"msg": "Success: {}, Fails: {}".format(success, failed), "job": job_id, "f": "ES Ingest"})
    return success, failed


def put_alias(client, index, alias_name, alias_body):
    client.indices.put_alias(index, alias_name, body=alias_body)


def create_aliases(client, index, silent=False):
    for award_type, award_type_codes in INDEX_ALIASES_TO_AWARD_TYPES.items():
        alias_name = "{}-{}".format(settings.ES_TRANSACTIONS_QUERY_ALIAS_PREFIX, award_type)
        if silent is False:
            printf(
                {
                    "msg": "Putting alias '{}' on {} with award codes {}".format(alias_name, index, award_type_codes),
                    "job": None,
                    "f": "ES Alias Put",
                }
            )
        alias_body = {"filter": {"terms": {"type": award_type_codes}}}
        put_alias(client, index, alias_name, alias_body)

    # ensure the new index is added to the alias used for incremental loads.
    # If the alias is on multiple indexes, the loads will fail!
    printf(
        {
            "msg": "Putting alias '{}' on {}".format(settings.ES_TRANSACTIONS_WRITE_ALIAS, index),
            "job": None,
            "f": "ES Alias Put",
        }
    )
    put_alias(client, index, settings.ES_TRANSACTIONS_WRITE_ALIAS, {})


def set_final_index_config(client, index):
    es_settingsfile = str(settings.APP_DIR / "etl" / "es_config_objects.json")
    with open(es_settingsfile) as f:
        settings_dict = json.load(f)
    final_index_settings = settings_dict["final_index_settings"]

    current_settings = client.indices.get(index)[index]["settings"]["index"]

    client.indices.put_settings(final_index_settings, index)
    client.indices.refresh(index)
    for setting, value in final_index_settings.items():
        message = 'Changing "{}" from {} to {}'.format(setting, current_settings.get(setting), value)
        printf({"msg": message, "job": None, "f": "ES Settings Put"})


def swap_aliases(client, index):
    if client.indices.get_alias(index, "*"):
        printf({"msg": 'Removing old aliases for index "{}"'.format(index), "job": None, "f": "ES Alias Drop"})
        client.indices.delete_alias(index, "_all")

    alias_patterns = settings.ES_TRANSACTIONS_QUERY_ALIAS_PREFIX + "*"
    old_indexes = []

    try:
        old_indexes = list(client.indices.get_alias("*", alias_patterns).keys())
        for old_index in old_indexes:
            client.indices.delete_alias(old_index, "_all")
            printf({"msg": 'Removing aliases from "{}"'.format(old_index), "job": None, "f": "ES Alias Drop"})
    except Exception:
        printf({"msg": "ERROR: no aliases found for {}".format(alias_patterns), "f": "ES Alias Drop"})

    create_aliases(client, index)

    try:
        if old_indexes:
            client.indices.delete(index=old_indexes, ignore_unavailable=False)
            printf({"msg": 'Deleted index(es) "{}"'.format(old_indexes), "job": None, "f": "ES Alias Drop"})
    except Exception:
        printf({"msg": "ERROR: Unable to delete indexes: {}".format(old_indexes), "f": "ES Alias Drop"})


def post_to_elasticsearch(client, job, config, chunksize=250000):
    printf({"msg": 'Populating ES Index "{}"'.format(job.index), "job": job.name, "f": "ES Ingest"})
    start = perf_counter()
    try:
        does_index_exist = client.indices.exists(job.index)
    except Exception as e:
        print(e)
        raise SystemExit(1)
    if not does_index_exist:
        printf({"msg": 'Creating index "{}"'.format(job.index), "job": job.name, "f": "ES Ingest"})
        client.indices.create(index=job.index)
        client.indices.refresh(job.index)

    csv_generator = csv_chunk_gen(job.csv, chunksize, job.name)
    for count, chunk in enumerate(csv_generator):
        if len(chunk) == 0:
            printf({"msg": "No documents to add/delete for chunk #{}".format(count), "f": "ES Ingest", "job": job.name})
            continue
        iteration = perf_counter()
        if config["process_deletes"]:
            id_list = [{"key": c[UNIVERSAL_TRANSACTION_ID_NAME], "col": UNIVERSAL_TRANSACTION_ID_NAME} for c in chunk]
            delete_transactions_from_es(client, id_list, job.name, config, job.index)

        current_rows = "({}-{})".format(count * chunksize + 1, count * chunksize + len(chunk))
        printf(
            {
                "msg": "Streaming to ES #{} rows [{}/{}]".format(count, current_rows, job.count),
                "job": job.name,
                "f": "ES Ingest",
            }
        )
        streaming_post_to_es(client, chunk, job.index, job.name)
        printf(
            {
                "msg": "Iteration group #{} took {}s".format(count, perf_counter() - iteration),
                "job": job.name,
                "f": "ES Ingest",
            }
        )
    printf(
        {
            "msg": "Elasticsearch Index loading took {}s".format(perf_counter() - start),
            "job": job.name,
            "f": "ES Ingest",
        }
    )


def deleted_transactions(client, config):
    deleted_ids = gather_deleted_ids(config)
    id_list = [{"key": deleted_id, "col": UNIVERSAL_TRANSACTION_ID_NAME} for deleted_id in deleted_ids]
    delete_transactions_from_es(client, id_list, None, config, None)


def take_snapshot(client, index, repository):
    snapshot_name = "{}-{}".format(index, str(datetime.now().date()))
    try:
        client.snapshot.create(repository, snapshot_name, body={"indices": index})
        printf(
            {
                "msg": 'Taking snapshot INDEX: "{}" SNAPSHOT: "{}" REPO: "{}"'.format(index, snapshot_name, repository),
                "f": "ES Snapshot",
            }
        )
    except TransportError as e:
        printf({"msg": 'SNAPSHOT "{}" FAILED'.format(str(e)), "f": "ES Snapshot"})
        raise SystemExit(1)


def gather_deleted_ids(config):
    """
    Connect to S3 and gather all of the transaction ids stored in CSV files
    generated by the broker when transactions are removed from the DB.
    """

    if not config["process_deletes"]:
        printf({"msg": "Skipping the S3 CSV fetch for deleted transactions"})
        return
    printf({"msg": "Gathering all deleted transactions from S3"})
    start = perf_counter()
    try:
        s3 = boto3.resource("s3", region_name=config["aws_region"])
        bucket = s3.Bucket(config["s3_bucket"])
        bucket_objects = list(bucket.objects.all())
    except Exception as e:
        print("\n[ERROR]\n")
        print("Verify settings.USASPENDING_AWS_REGION and settings.DELETED_TRANSACTIONS_S3_BUCKET_NAME are correct")
        print("  or is using env variables: USASPENDING_AWS_REGION and DELETED_TRANSACTIONS_S3_BUCKET_NAME")
        print("\n {} \n".format(e))
        raise SystemExit(1)

    if config["verbose"]:
        printf({"msg": "CSV data from {} to now".format(config["starting_date"])})

    filtered_csv_list = [
        x
        for x in bucket_objects
        if (x.key.endswith(".csv") and not x.key.startswith("staging") and x.last_modified >= config["starting_date"])
    ]

    if config["verbose"]:
        printf({"msg": "Found {} csv files".format(len(filtered_csv_list))})

    deleted_ids = {}

    for obj in filtered_csv_list:
        # Use temporary files to facilitate date moving from csv files on S3 into pands
        (file, file_path) = tempfile.mkstemp()
        bucket.download_file(obj.key, file_path)

        # Ingests the CSV into a dataframe. pandas thinks some ids are dates, so disable parsing
        data = pd.read_csv(file_path, dtype=str)

        if "detached_award_proc_unique" in data:
            new_ids = ["CONT_TX_" + x.upper() for x in data["detached_award_proc_unique"].values]
        elif "afa_generated_unique" in data:
            new_ids = ["ASST_TX_" + x.upper() for x in data["afa_generated_unique"].values]
        else:
            printf({"msg": "  [Missing valid col] in {}".format(obj.key)})

        # Next statements are ugly, but properly handle the temp files
        os.close(file)
        os.remove(file_path)

        for uid in new_ids:
            if uid in deleted_ids:
                if deleted_ids[uid]["timestamp"] < obj.last_modified:
                    deleted_ids[uid]["timestamp"] = obj.last_modified
            else:
                deleted_ids[uid] = {"timestamp": obj.last_modified}

    if config["verbose"]:
        for uid, deleted_dict in deleted_ids.items():
            printf({"msg": "id: {} last modified: {}".format(uid, str(deleted_dict["timestamp"]))})

    printf({"msg": "Gathering {} deleted transactions took {}s".format(len(deleted_ids), perf_counter() - start)})
    return deleted_ids


def filter_query(column, values, query_type="match_phrase"):
    queries = [{query_type: {column: str(i)}} for i in values]
    return {"query": {"bool": {"should": [queries]}}}


def delete_query(response):
    return {"query": {"ids": {"type": "transaction_mapping", "values": [i["_id"] for i in response["hits"]["hits"]]}}}


def chunks(l, n):
    """Yield successive n-sized chunks from l."""
    for i in range(0, len(l), n):
        yield l[i : i + n]


def delete_transactions_from_es(client, id_list, job_id, config, index=None):
    """
    id_list = [{key:'key1',col:'tranaction_id'},
               {key:'key2',col:'generated_unique_transaction_id'}],
               ...]
    """
    start = perf_counter()

    printf({"msg": "Deleting up to {} document(s)".format(len(id_list)), "f": "ES Delete", "job": job_id})

    if index is None:
        index = "{}-*".format(config["root_index"])
    start_ = client.search(index=index)["hits"]["total"]
    printf({"msg": "Starting amount of indices ----- {}".format(start_), "f": "ES Delete", "job": job_id})
    col_to_items_dict = defaultdict(list)
    for l in id_list:
        col_to_items_dict[l["col"]].append(l["key"])

    for column, values in col_to_items_dict.items():
        printf({"msg": 'Deleting {} of "{}"'.format(len(values), column), "f": "ES Delete", "job": job_id})
        values_generator = chunks(values, 1000)
        for v in values_generator:
            # IMPORTANT: This delete routine looks at just 1 index at a time. If there are duplicate records across
            # multiple indexes, those duplicates will not be caught by this routine. It is left as is because at the
            # time of this comment, we are migrating to using a single index.
            body = filter_query(column, v)
            response = client.search(index=index, body=json.dumps(body), size=config["max_query_size"])
            delete_body = delete_query(response)
            try:
                client.delete_by_query(
                    index=index, body=json.dumps(delete_body), refresh=True, size=config["max_query_size"]
                )
            except Exception as e:
                printf({"msg": "[ERROR][ERROR][ERROR]\n{}".format(str(e)), "f": "ES Delete", "job": job_id})
    end_ = client.search(index=index)["hits"]["total"]

    t = perf_counter() - start
    total = str(start_ - end_)
    printf({"msg": "ES Deletes took {}s. Deleted {} records".format(t, total), "f": "ES Delete", "job": job_id})
    return


def printf(items):
    t = datetime.utcnow().strftime("%H:%M:%S.%f")
    msg = items["msg"]
    if "error" in items:
        template = "[{time}] [ERROR] {msg}"
        print_msg = template.format(time=t, msg=msg)
    else:
        template = "[{time}] {complex:<20} | {msg}"
        func = "[" + items.get("f", "main") + "]"
        job = items.get("job", None)
        j = ""
        if job:
            j = " (#{})".format(job)
        print_msg = template.format(time=t, complex=func + j, msg=msg)
    print(print_msg)
