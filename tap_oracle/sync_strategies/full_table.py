#!/usr/bin/env python3
import copy
import decimal
import pdb
import time
import os

import singer
import singer.metadata as metadata
import singer.metrics as metrics
import tap_oracle.db as orc_db
import tap_oracle.sync_strategies.common as common
from singer import get_bookmark, utils, write_message
from singer.schema import Schema

LOGGER = singer.get_logger()

UPDATE_BOOKMARK_PERIOD = 1000

BATCH_SIZE = 1000

USE_ORA_ROWSCN = True

def sync_view(conn_config, stream, state, desired_columns):
   connection = orc_db.open_connection(conn_config)
   connection.outputtypehandler = common.OutputTypeHandler

   cur = connection.cursor()
   cur.arraysize = BATCH_SIZE
   cur.execute("ALTER SESSION SET TIME_ZONE = '00:00'")
   cur.execute("""ALTER SESSION SET NLS_DATE_FORMAT = 'YYYY-MM-DD"T"HH24:MI:SS."00+00:00"'""")
   cur.execute("""ALTER SESSION SET NLS_TIMESTAMP_FORMAT='YYYY-MM-DD"T"HH24:MI:SSXFF"+00:00"'""")
   cur.execute("""ALTER SESSION SET NLS_TIMESTAMP_TZ_FORMAT  = 'YYYY-MM-DD"T"HH24:MI:SS.FFTZH:TZM'""")
   time_extracted = utils.now()

   #before writing the table version to state, check if we had one to begin with
   first_run = singer.get_bookmark(state, stream.tap_stream_id, 'version') is None

   #pick a new table version
   nascent_stream_version = int(time.time() * 1000)
   state = singer.write_bookmark(state,
                                 stream.tap_stream_id,
                                 'version',
                                 nascent_stream_version)
   singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))

   # cur = connection.cursor()
   md = metadata.to_map(stream.metadata)
   schema_name = md.get(()).get('schema-name')

   escaped_columns = map(lambda c: common.prepare_columns_sql(stream, c), desired_columns)
   escaped_schema  = schema_name
   escaped_table   = stream.table
   activate_version_message = singer.ActivateVersionMessage(
      stream=stream.tap_stream_id,
      version=nascent_stream_version)

   if first_run:
      singer.write_message(activate_version_message)

   with metrics.record_counter(None) as counter:
     
      counter.tags["schema"] = escaped_schema
      counter.tags["table"] = escaped_table

      select_sql      = 'SELECT {} FROM {}.{}'.format(','.join(escaped_columns),
                                                      escaped_schema,
                                                      escaped_table)

      LOGGER.info("select %s", select_sql)
      for index, row in enumerate(cur.execute(select_sql), 1):
         record_message = common.row_to_singer_message(stream,
                                                       row,
                                                       nascent_stream_version,
                                                       desired_columns,
                                                       time_extracted)
         singer.write_message(record_message)
         #singer.write_message(singer.StateMessage(value=index))
         counter.increment()

   #always send the activate version whether first run or subsequent
   singer.write_message(activate_version_message)

   state['record_count'] = index


   #os.environ('RECORD_COUNT') = index
   

   singer.write_message(singer.StateMessage(value=index))
   record_count = index
   cur.close()
   connection.close()
   return state

def sync_table(conn_config, stream, state, desired_columns):
   connection = orc_db.open_connection(conn_config)
   connection.outputtypehandler = common.OutputTypeHandler

   cur = connection.cursor()
   cur.arraysize = BATCH_SIZE
   cur.execute("ALTER SESSION SET TIME_ZONE = '00:00'")
   cur.execute("""ALTER SESSION SET NLS_DATE_FORMAT = 'YYYY-MM-DD"T"HH24:MI:SS."00+00:00"'""")
   cur.execute("""ALTER SESSION SET NLS_TIMESTAMP_FORMAT='YYYY-MM-DD"T"HH24:MI:SSXFF"+00:00"'""")
   cur.execute("""ALTER SESSION SET NLS_TIMESTAMP_TZ_FORMAT  = 'YYYY-MM-DD"T"HH24:MI:SS.FFTZH:TZM'""")
   time_extracted = utils.now()

   #before writing the table version to state, check if we had one to begin with
   first_run = singer.get_bookmark(state, stream.tap_stream_id, 'version') is None

   #pick a new table version IFF we do not have an ORA_ROWSCN in our state
   #the presence of an ORA_ROWSCN indicates that we were interrupted last time through
   if singer.get_bookmark(state, stream.tap_stream_id, 'ORA_ROWSCN') is None:
      nascent_stream_version = int(time.time() * 1000)
   else:
      nascent_stream_version = singer.get_bookmark(state, stream.tap_stream_id, 'version')

   state = singer.write_bookmark(state,
                                 stream.tap_stream_id,
                                 'version',
                                 nascent_stream_version)
   singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))

   # cur = connection.cursor()
   md = metadata.to_map(stream.metadata)
   schema_name = md.get(()).get('schema-name')

   escaped_columns = map(lambda c: common.prepare_columns_sql(stream, c), desired_columns)
   escaped_schema  = schema_name
   escaped_table   = stream.table
   activate_version_message = singer.ActivateVersionMessage(
      stream=stream.tap_stream_id,
      version=nascent_stream_version)

   if first_run:
      singer.write_message(activate_version_message)

   with metrics.record_counter(None) as counter:
     
      counter.tags["schema"] = escaped_schema
      counter.tags["table"] = escaped_table
     
      ora_rowscn = singer.get_bookmark(state, stream.tap_stream_id, 'ORA_ROWSCN')
      if not USE_ORA_ROWSCN:
         # Warning there is not restart recovery if the ORA_ROWSCN is ignored.
         select_sql      = """SELECT {}, NULL as ORA_ROWSCN
                                FROM {}.{}""".format(','.join(escaped_columns),
                                           escaped_schema,
                                           escaped_table)
      elif ora_rowscn:
         LOGGER.info("Resuming Full Table replication %s from ORA_ROWSCN %s", nascent_stream_version, ora_rowscn)
         select_sql      = """SELECT {}, ORA_ROWSCN
                                FROM {}.{}
                               WHERE ORA_ROWSCN >= {}
                               ORDER BY ORA_ROWSCN ASC
                                """.format(','.join(escaped_columns),
                                           escaped_schema,
                                           escaped_table,
                                           ora_rowscn)
      else:
         select_sql      = """SELECT {}, ORA_ROWSCN
                                FROM {}.{}
                               ORDER BY ORA_ROWSCN ASC""".format(','.join(escaped_columns),
                                                                    escaped_schema,
                                                                    escaped_table)

      rows_saved = 0
      LOGGER.info("select %s", select_sql)
      for row in cur.execute(select_sql):
         ora_rowscn = row[-1]
         row = row[:-1]
         record_message = common.row_to_singer_message(stream,
                                                       row,
                                                       nascent_stream_version,
                                                       desired_columns,
                                                       time_extracted)

         singer.write_message("Test")
         singer.write_message(record_message)
         state = singer.write_bookmark(state, stream.tap_stream_id, 'ORA_ROWSCN', ora_rowscn)
         rows_saved = rows_saved + 1
         if rows_saved % UPDATE_BOOKMARK_PERIOD == 0:
            singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))

         counter.increment()

   state = singer.write_bookmark(state, stream.tap_stream_id, 'ORA_ROWSCN', None)
   #always send the activate version whether first run or subsequent
   singer.write_message(activate_version_message)
   cur.close()
   connection.close()
   return state
