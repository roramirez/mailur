import uuid

import psycopg2
import psycopg2.extras
import rapidjson as json

psycopg2.extensions.register_adapter(dict, psycopg2.extras.Json)
psycopg2.extensions.register_adapter(uuid.UUID, psycopg2.extras.UUID_adapter)


def init(env, password=None, reset=False):
    if reset:
        with env.db_connect(dbname='postgres') as conn:
            conn.set_isolation_level(0)
            with conn.cursor() as cur:
                cur.execute('DROP DATABASE IF EXISTS %s' % env.db_name)
                cur.execute('CREATE DATABASE %s' % env.db_name)

    sql = '''
    CREATE EXTENSION IF NOT EXISTS "pgcrypto";
    CREATE OR REPLACE FUNCTION fill_updated()
    RETURNS TRIGGER AS $$
    BEGIN
       IF row(NEW.*) IS DISTINCT FROM row(OLD.*) THEN
          NEW.updated = now();
          RETURN NEW;
       ELSE
          RETURN OLD;
       END IF;
    END;
    $$ language 'plpgsql';
    '''
    sql += ';'.join(t.table for t in [Storage, Emails])
    env.sql(sql)
    env.db.commit()

    if password:
        env.set_password(password)
    elif reset:
        token = env.set_password(reset=True)
        print('Reset password: /pwd/%s/%s/' % (env.username, token))


def fill_updated(table, field='updated'):
    return '''
    DROP TRIGGER IF EXISTS fill_{0}_{1} ON {0};
    CREATE TRIGGER fill_{0}_{1} BEFORE UPDATE ON {0}
       FOR EACH ROW EXECUTE PROCEDURE fill_updated()
    '''.format(table, field)


def create_index(table, field, using=''):
    if using:
        using = 'USING %s ' % using
    return '''
    DROP INDEX IF EXISTS ix_{0}_{1};
    CREATE INDEX ix_{0}_{1} ON {0} {2}({1})
    '''.format(table, field, using)


def create_seq(table, field):
    return '''
    DO $$
    BEGIN
        CREATE SEQUENCE seq_{0}_{1};
        ALTER TABLE {0} ALTER COLUMN {1} SET DEFAULT nextval('seq_{0}_{1}');
    EXCEPTION WHEN duplicate_table THEN
        -- already exists
    END$$;
    '''.format(table, field)


def create_table(name, body, before=None, after=None):
    sql = ['CREATE TABLE IF NOT EXISTS %s (%s)' % (name, ', '.join(body))]
    before, after = (
        [v] if isinstance(v, str) else list(v or [])
        for v in (before, after)
    )
    return '; '.join(before + sql + after)


class Manager():
    pk = 'id'

    def __init__(self, env):
        self.env = env
        self.field_names = tuple(f.split()[0].strip('"') for f in self.fields)

        # bind sql functions directly to obj
        self.sql = env.sql
        self.sqlmany = env.sqlmany
        self.mogrify = env.mogrify

    @property
    def db(self):
        return self.env.db

    def get_field(self, name):
        if name not in self.field_names:
            raise ValueError('Wrong field: %s', name)
        field = [
            i for i in self.fields
            if i.startswith(name) or i.startswith('"' + name)
        ][0]
        return field

    def get_fields(self, fields):
        fields = sorted(f for f in fields)
        error = set(fields) - set(self.field_names)
        if error:
            raise ValueError('No fields: %s' % error)
        return fields

    def sql_fields(self, fields):
        fields = self.get_fields(fields)
        return '(%s)' % ', '.join('"%s"' % i for i in fields)

    def sql_values(self, items):
        if isinstance(items, dict):
            items = [items]

        fields = self.get_fields(items[0])
        pattern = '(%s)' % ', '.join('%%(%s)s' % i for i in fields)
        values = ','.join([self.mogrify(pattern, v) for v in items])
        return values

    def insert(self, items):
        if isinstance(items, dict):
            items = [items]

        i = self.sql('''
        INSERT INTO {table} {fields} VALUES {values} RETURNING {pk}
        '''.format(
            table=self.name,
            pk=self.pk,
            fields=self.sql_fields(items[0].keys()),
            values=self.sql_values(items)
        ))
        return [r[0] for r in i]

    def update(self, values, where, params=None):
        i = self.sql('''
        UPDATE {table} SET {fields} = {values}
            WHERE {where}
            RETURNING {pk}
        '''.format(
            table=self.name,
            pk=self.pk,
            fields=self.sql_fields(values.keys()),
            values=self.sql_values(values),
            where=self.mogrify(where, params)
        ))
        return [r[0] for r in i]

    def upsert(self, values, where, params=None):
        return self.sql('''
        UPDATE {table} SET {fields} = {values} WHERE {where};
        INSERT INTO {table} {fields}
            SELECT {select}
            WHERE NOT EXISTS (SELECT 1 FROM {table} WHERE {where});
        '''.format(
            table=self.name,
            fields=self.sql_fields(values.keys()),
            values=self.sql_values(values),
            select=self.sql_values(values)[1:-1:],
            where=self.mogrify(where, params)
        ))


class Key():
    def __init__(self, storage, key):
        self.storage = storage
        self.key = key

    def get(self, default=None):
        if '_val' not in self.__dict__:
            self.__dict__['_val'] = self.storage.get(self.key, default)
        return self.__dict__['_val']

    def set(self, value):
        self.__dict__.pop('_val', None)
        return self.storage.set(self.key, value)

    def rm(self):
        self.__dict__.pop('_val', None)
        return self.storage.rm(self.key)


class Storage(Manager):
    name = 'storage'
    pk = 'key'
    fields = (
        'key varchar PRIMARY KEY',
        'value jsonb',

        'created timestamp NOT NULL DEFAULT current_timestamp',
        'updated timestamp NOT NULL DEFAULT current_timestamp'
    )
    table = create_table(name, fields, after=(
        fill_updated(name),
    ))

    def get(self, key, default=None):
        value = self.sql('''
        SELECT value FROM storage WHERE key=%s
        ''', (key,)).fetchall()
        value = value and value[0][0]
        return value or default

    def set(self, key, value):
        value = json.dumps(value, ensure_ascii=False)
        self.upsert({'key': key, 'value': value}, 'key=%s', [key])
        self.db.commit()

    def rm(self, key):
        self.sql('DELETE FROM storage WHERE key=%s', [key])
        self.db.commit()

    def __call__(self, name, **params):
        targets = {
            'compose': lambda thrid: 'compose:%s' % (thrid or 'new'),
            'folder': lambda uid: 'folder:%s' % uid,
        }
        return Key(self, targets[name](**params))


class Emails(Manager):
    name = 'emails'
    fields = (
        'id bigint PRIMARY KEY',
        'created timestamp NOT NULL DEFAULT current_timestamp',
        'updated timestamp NOT NULL DEFAULT current_timestamp',
        'msgid varchar UNIQUE',
        'extid varchar UNIQUE',
        'delid uuid NOT NULL',  # remove after migrating

        'thrid bigint REFERENCES emails(id)',
        'parent bigint REFERENCES emails(id)',
        'duplicate bigint REFERENCES emails(id)',

        'header bytea',
        'raw bytea',
        'size int',
        'time timestamp',
        "labels varchar[] NOT NULL DEFAULT '{}'",

        'subj varchar',
        "fr varchar[] NOT NULL DEFAULT '{}'",
        '"to" varchar[] NOT NULL DEFAULT \'{}\'',
        "cc varchar[] NOT NULL DEFAULT '{}'",
        "bcc varchar[] NOT NULL DEFAULT '{}'",
        "reply_to varchar[] NOT NULL DEFAULT '{}'",
        "sender varchar[] NOT NULL DEFAULT '{}'",
        'sender_time timestamp',
        'in_reply_to varchar',
        "refs varchar[] NOT NULL DEFAULT '{}'",

        'text text',
        'html text',
        "attachments jsonb",
        "embedded jsonb",

        'search tsvector',
    )
    table = create_table(name, fields, after=(
        fill_updated(name),
        create_seq(name, 'id'),
        create_index(name, 'size'),
        create_index(name, 'msgid'),
        create_index(name, 'thrid'),
        create_index(name, 'extid'),
        create_index(name, 'in_reply_to'),
        create_index(name, 'refs', 'GIN'),
        create_index(name, 'labels', 'GIN'),
        create_index(name, 'search', 'GIN')
    ))
