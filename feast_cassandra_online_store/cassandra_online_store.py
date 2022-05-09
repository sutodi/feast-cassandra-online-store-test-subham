"""
Cassandra/Astra DB online store for Feast.
"""

import logging
from datetime import datetime
from typing import (Sequence, List, Optional, Tuple, Dict, Callable,
                    Any, Iterable)

from feast import RepoConfig, FeatureView, Entity
from feast.infra.key_encoding_utils import serialize_entity_key
from feast.infra.online_stores.online_store import OnlineStore
from feast.protos.feast.types.EntityKey_pb2 import EntityKey as EntityKeyProto
from feast.protos.feast.types.Value_pb2 import Value as ValueProto
from feast.repo_config import FeastConfigBaseModel
from feast.usage import log_exceptions_and_usage, tracing_span

from pydantic import StrictStr, StrictInt
from pydantic.typing import Literal

from cassandra.cluster import Cluster, Session, ResultSet
from cassandra.auth import PlainTextAuthProvider


# Error messages
E_CASSANDRA_UNEXPECTED_CONFIGURATION_CLASS = (
    "Unexpected configuration object (not a "
    "CassandraOnlineStoreConfig instance)"
)
E_CASSANDRA_NOT_CONFIGURED = (
    "Inconsistent Cassandra configuration: provide exactly one between "
    "'hosts' and 'secure_bundle_path' and a 'keyspace'"
)
E_CASSANDRA_MISCONFIGURED = (
    "Inconsistent Cassandra configuration: provide either 'hosts' or "
    "'secure_bundle_path', not both"
)
E_CASSANDRA_INCONSISTENT_AUTH = (
    "Username and password for Cassandra must be provided either both or none"
)

# CQL command templates (that is, before replacing schema names)
INSERT_CQL_4_TEMPLATE = ("INSERT INTO {fqtable} (feature_name,"
                         " value, entity_key, event_ts) VALUES"
                         " (?, ?, ?, ?);")

INSERT_CQL_5_TEMPLATE = ("INSERT INTO {fqtable} (feature_name, "
                         "value, entity_key, event_ts, created_ts)"
                         " VALUES (?, ?, ?, ?, ?);")

SELECT_CQL_TEMPLATE = ("SELECT {columns} FROM {fqtable}"
                       " WHERE entity_key = ?;")

CREATE_TABLE_CQL_TEMPLATE = """
    CREATE TABLE IF NOT EXISTS {fqtable} (
        entity_key      TEXT,
        feature_name    TEXT,
        value           BLOB,
        event_ts        TIMESTAMP,
        created_ts      TIMESTAMP,
        PRIMARY KEY ((entity_key), feature_name)
    ) WITH CLUSTERING ORDER BY (feature_name ASC);
"""

DROP_TABLE_CQL_TEMPLATE = "DROP TABLE IF EXISTS {fqtable};"

# op_name -> (cql template string, prepare boolean)
CQL_TEMPLATE_MAP = {
    # Queries/DML, statements to be prepared
    'insert4': (INSERT_CQL_4_TEMPLATE, True),
    'insert5': (INSERT_CQL_5_TEMPLATE, True),
    'select': (SELECT_CQL_TEMPLATE, True),
    # DDL, do not prepare these
    'drop': (DROP_TABLE_CQL_TEMPLATE, False),
    'create': (CREATE_TABLE_CQL_TEMPLATE, False),
}

# Logger
logger = logging.getLogger(__name__)


class CassandraInvalidConfig(Exception):
    def __init__(self, msg: str):
        super().__init__(msg)


class CassandraOnlineStoreConfig(FeastConfigBaseModel):
    """
    Configuration for the Cassandra/Astra DB online store.

    Exactly one of `hosts` and `secure_bundle_path` must be provided;
    depending on which one, the connection will be to a regular Cassandra
    or an Astra DB instance (respectively).

    If connecting to Astra DB, authentication must be provided with username
    and password being the Client ID and Client Secret of the database token.
    """
    _full_class_name = ("feast_cassandra_online_store.cassandra_online_store"
                        ".CassandraOnlineStore")
    #
    type: Literal["cassandra", _full_class_name] = _full_class_name
    """Online store type selector."""

    # settings for connection to Cassandra / Astra DB
    hosts: Optional[List[StrictStr]] = None
    """List of host addresses to reach the cluster."""

    secure_bundle_path: Optional[StrictStr] = None
    """Path to the secure connect bundle (for Astra DB; replaces hosts)."""

    port: Optional[StrictInt] = None
    """Port number for connecting to the cluster (optional)."""

    keyspace: StrictStr = None
    """Target Cassandra keyspace where all tables will be."""

    username: Optional[StrictStr] = None
    """Username for DB auth, possibly Astra DB token Client ID."""

    password: Optional[StrictStr] = None
    """Password for DB auth, possibly Astra DB token Client Secret."""


class CassandraOnlineStore(OnlineStore):
    """
    Cassandra/Astra DB online store implementation for Feast.

    Attributes:
        _cluster:   Cassandra cluster to connect to.
        _session:   (DataStax Cassandra drivers) session object
                    to issue commands.
        _keyspace:  Cassandra keyspace all tables live in.
        _prepared_statements: cache of statements prepared by the driver.
    """

    _cluster: Cluster = None
    _session: Session = None
    _keyspace: str = None
    _prepared_statements = {}

    def _get_session(self, config: RepoConfig):
        """
        Establish the database connection, if not yet created,
        and return it.

        Also perform basic config validation checks.
        """

        online_store_config = config.online_store
        if not isinstance(online_store_config, CassandraOnlineStoreConfig):
            raise CassandraInvalidConfig(
                E_CASSANDRA_UNEXPECTED_CONFIGURATION_CLASS
            )

        if self._session:
            return self._session
        if not self._session:
            # configuration consistency checks
            hosts = online_store_config.hosts
            secure_bundle_path = online_store_config.secure_bundle_path
            port = online_store_config.port or 9042
            keyspace = online_store_config.keyspace
            username = online_store_config.username
            password = online_store_config.password

            db_directions = hosts or secure_bundle_path
            if not db_directions or not keyspace:
                raise CassandraInvalidConfig(E_CASSANDRA_NOT_CONFIGURED)
            if hosts and secure_bundle_path:
                raise CassandraInvalidConfig(E_CASSANDRA_MISCONFIGURED)
            if (username is None) ^ (password is None):
                raise CassandraInvalidConfig(E_CASSANDRA_INCONSISTENT_AUTH)

            if username is not None:
                auth_provider = PlainTextAuthProvider(
                    username=username,
                    password=password,
                )
            else:
                auth_provider = None

            # creation of Cluster (Cassandra vs. Astra)
            if hosts:
                self._cluster = Cluster(
                    hosts,
                    port=port,
                    auth_provider=auth_provider,
                )
            else:
                # we use 'secure_bundle_path'
                self._cluster = Cluster(
                    cloud={
                        "secure_connect_bundle": secure_bundle_path,
                    },
                    auth_provider=auth_provider,
                )

            # creation of Session
            self._keyspace = keyspace
            self._session = self._cluster.connect(self._keyspace)

        return self._session

    @log_exceptions_and_usage(online_store="cassandra")
    def online_write_batch(
            self,
            config: RepoConfig,
            table: FeatureView,
            data: List[
                Tuple[EntityKeyProto,
                      Dict[str, ValueProto], datetime, Optional[datetime]]
            ],
            progress: Optional[Callable[[int], Any]],
    ) -> None:
        """
        Write a batch of features of several entities to the database.

        Args:
            config: The RepoConfig for the current FeatureStore.
            table: Feast FeatureView.
            data: a list of quadruplets containing Feature data. Each
                  quadruplet contains an Entity Key, a dict containing feature
                  values, an event timestamp for the row, and
                  the created timestamp for the row if it exists.
            progress: Optional function to be called once every mini-batch of
                      rows is written to the online store. Can be used to
                      display progress.
        """
        project = config.project
        #
        for entity_key, values, timestamp, created_ts in data:
            entity_key_bin = serialize_entity_key(entity_key).hex()
            with tracing_span(name="remote_call"):
                self._write_rows(config, project, table, entity_key_bin,
                                 values.items(), timestamp, created_ts)
            if progress:
                progress(1)

    @log_exceptions_and_usage(online_store="cassandra")
    def online_read(
            self,
            config: RepoConfig,
            table: FeatureView,
            entity_keys: List[EntityKeyProto],
            requested_features: Optional[List[str]] = None,
    ) -> List[Tuple[Optional[datetime], Optional[Dict[str, ValueProto]]]]:
        """
        Read feature values pertaining to the requested entities from
        the online store.

        Args:
            config: The RepoConfig for the current FeatureStore.
            table: Feast FeatureView.
            entity_keys: a list of entity keys that should be read
                         from the FeatureStore.
        """
        project = config.project

        result: List[Tuple[Optional[datetime],
                     Optional[Dict[str, ValueProto]]]] = []

        for entity_key in entity_keys:
            entity_key_bin = serialize_entity_key(entity_key).hex()

            with tracing_span(name="remote_call"):
                feature_rows = self._read_rows_by_entity_key(
                    config, project, table, entity_key_bin,
                    proj=["feature_name", "value", "event_ts"],
                )

            res = {}
            res_ts = None
            for feature_row in feature_rows:
                if (requested_features is None
                        or feature_row.feature_name in requested_features):
                    val = ValueProto()
                    val.ParseFromString(feature_row.value)
                    res[feature_row.feature_name] = val
                    res_ts = feature_row.event_ts
            #
            if not res:
                result.append((None, None))
            else:
                result.append((res_ts, res))
        return result

    @log_exceptions_and_usage(online_store="cassandra")
    def update(
            self,
            config: RepoConfig,
            tables_to_delete: Sequence[FeatureView],
            tables_to_keep: Sequence[FeatureView],
            entities_to_delete: Sequence[Entity],
            entities_to_keep: Sequence[Entity],
            partial: bool,
    ):
        """
        Update schema on DB, by creating and destroying tables accordingly.

        Args:
            config: The RepoConfig for the current FeatureStore.
            tables_to_delete: Tables to delete from the Online Store.
            tables_to_keep: Tables to keep in the Online Store.
        """
        project = config.project

        for table in tables_to_keep:
            with tracing_span(name="remote_call"):
                self._create_table(config, project, table)
        for table in tables_to_delete:
            with tracing_span(name="remote_call"):
                self._drop_table(config, project, table)

    @log_exceptions_and_usage(online_store="cassandra")
    def teardown(
            self,
            config: RepoConfig,
            tables: Sequence[FeatureView],
            entities: Sequence[Entity],
    ):
        """
        Delete tables from the database.

        Args:
            config: The RepoConfig for the current FeatureStore.
            tables: Tables to delete from the feature repo.
        """
        project = config.project

        for table in tables:
            with tracing_span(name="remote_call"):
                self._drop_table(config, project, table)

    @staticmethod
    def _fq_table_name(
        keyspace: str,
        project: str,
        table: FeatureView,
    ) -> str:
        """
        Generate a fully-qualified table name,
        including quotes and keyspace.
        """
        return f"\"{keyspace}\".\"{project}_{table.name}\""

    def _write_rows(
        self,
        config: RepoConfig,
        project: str,
        table: FeatureView,
        entity_key_bin: bytes,
        features_vals: Iterable[Tuple[str, ValueProto]],
        timestamp: datetime,
        created_ts: Optional[datetime],
    ):
        """
        Handle the CQL (low-level) insertion of feature values to a table.

        Note: `created_ts` can be None: in that case we avoid explicitly
        inserting it to prevent unnecessary tombstone creation on Cassandra.
        """
        session: Session = self._get_session(config)
        keyspace: str = self._keyspace
        #
        fqtable = CassandraOnlineStore._fq_table_name(keyspace, project, table)
        if created_ts is None:
            insert_cql = self._get_cql_statement(
                config,
                'insert4',
                fqtable=fqtable,
            )
            fixed_vals = [entity_key_bin, timestamp]
        else:
            insert_cql = self._get_cql_statement(
                config,
                'insert5',
                fqtable=fqtable,
            )
            fixed_vals = [entity_key_bin, timestamp, created_ts]
        #
        for feature_name, val in features_vals:
            session.execute(
                insert_cql,
                [feature_name, val.SerializeToString()] + fixed_vals,
            )

    def _read_rows_by_entity_key(
        self,
        config: RepoConfig,
        project: str,
        table: FeatureView,
        entity_key_bin: bytes,
        proj: Optional[List[str]] = None,
    ) -> ResultSet:
        """
        Handle the CQL (low-level) reading of feature values from a table.
        """
        session: Session = self._get_session(config)
        keyspace: str = self._keyspace
        #
        fqtable = CassandraOnlineStore._fq_table_name(keyspace, project, table)
        columns="*" if proj is None else ", ".join(proj)
        select_cql = self._get_cql_statement(
            config,
            'select',
            fqtable=fqtable,
            columns=columns,
        )
        return session.execute(select_cql, [entity_key_bin])

    def _drop_table(
        self,
        config: RepoConfig,
        project: str,
        table: FeatureView,
    ):
        """Handle the CQL (low-level) deletion of a table."""
        session: Session = self._get_session(config)
        keyspace: str = self._keyspace
        #
        fqtable = CassandraOnlineStore._fq_table_name(keyspace, project, table)
        drop_cql = self._get_cql_statement(config, 'drop', fqtable)
        logger.info(f"Deleting table {fqtable}.")
        session.execute(drop_cql)

    def _create_table(
        self,
        config: RepoConfig,
        project: str,
        table: FeatureView,
    ):
        """Handle the CQL (low-level) creation of a table."""
        session: Session = self._get_session(config)
        keyspace: str = self._keyspace
        #
        fqtable = CassandraOnlineStore._fq_table_name(keyspace, project, table)
        create_cql = self._get_cql_statement(config, 'create', fqtable)
        logger.info(f"Creating table {fqtable}.")
        session.execute(create_cql)

    def _get_cql_statement(
        self,
        config: RepoConfig,
        op_name: str,
        fqtable: str,
        **kwargs,
    ):
        """
        Resolve an 'op_name' (create, insert4, etc) into a CQL statement
        ready to be bound to parameters when executing.

        If the statement is defined to be 'prepared', use an instance-specific
        cache of prepared statements.

        This additional layer makes it easy to control whether to use prepared
        statements and, if so, on which database operations.
        """
        session: Session = self._get_session(config)
        #
        template, prepare = CQL_TEMPLATE_MAP[op_name]
        statement = template.format(
            fqtable=fqtable,
            **kwargs,
        )
        if prepare:
            cache_key = tuple(
                [op_name, fqtable] +
                [
                    '%s@%s' % (str(v), str(k))
                    for k, v in sorted(kwargs.items())
                ]
            )
            if cache_key not in self._prepared_statements:
                logger.info(f"Preparing a {op_name} statement on {fqtable}.")
                self._prepared_statements[cache_key] = \
                    session.prepare(statement)
            return self._prepared_statements[cache_key]
        else:
            return statement
