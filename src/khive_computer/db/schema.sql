-- The computer's SQLite store: one file, computer.db, in the data directory. Every table is created
-- with IF NOT EXISTS, so opening an older computer with a newer package adds what is missing and
-- changes nothing that exists; a change to an existing column is a record in docs/adr/, not an edit here.

-- A seat: one identity that acts, one token (stored hashed, never in the clear).
CREATE TABLE IF NOT EXISTS seats(
  id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, token_hash TEXT UNIQUE NOT NULL,
  identity TEXT NOT NULL DEFAULT '', system_prompt TEXT NOT NULL DEFAULT '',
  loop_state TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL);

-- A ref: the only mutable name; points at a tree id. rev counts its moves. A branch is a ref with a base.
CREATE TABLE IF NOT EXISTS refs(
  name TEXT PRIMARY KEY, tree TEXT NOT NULL, rev INTEGER NOT NULL DEFAULT 0,
  base_ref TEXT, base_tree TEXT, updated_at REAL NOT NULL);

-- One row per move of a ref: who, when, from which tree, to which tree.
CREATE TABLE IF NOT EXISTS ref_history(
  name TEXT NOT NULL, rev INTEGER NOT NULL, old_tree TEXT, new_tree TEXT NOT NULL,
  actor TEXT, note TEXT, at REAL NOT NULL, PRIMARY KEY(name, rev));

-- The result cache: an action keyed by its inputs, with what it produced.
CREATE TABLE IF NOT EXISTS actions(
  key TEXT PRIMARY KEY, tree_in TEXT NOT NULL, argv TEXT NOT NULL, cwd TEXT NOT NULL,
  env TEXT NOT NULL, sandbox TEXT NOT NULL, tree_out TEXT, exit_code INTEGER,
  stdout TEXT, stderr TEXT, ms INTEGER, actor TEXT, at REAL NOT NULL);

-- A snapshot: every ref's (tree, rev) at a moment, as one JSON map under a name.
CREATE TABLE IF NOT EXISTS snapshots(
  name TEXT PRIMARY KEY, refs TEXT NOT NULL, actor TEXT, note TEXT, at REAL NOT NULL);

-- One row per tool call: the seat, the verb (family.action), the arguments, outcome, latency, bytes.
CREATE TABLE IF NOT EXISTS events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, actor TEXT, verb TEXT NOT NULL,
  args TEXT, ok INTEGER NOT NULL, ms INTEGER NOT NULL,
  bytes_in INTEGER NOT NULL DEFAULT 0, bytes_out INTEGER NOT NULL DEFAULT 0, error TEXT);

-- A repository imported into the computer: its clone under repos/ and where it came from.
CREATE TABLE IF NOT EXISTS repos(
  name TEXT PRIMARY KEY, path TEXT NOT NULL, src TEXT, created_at REAL NOT NULL);
