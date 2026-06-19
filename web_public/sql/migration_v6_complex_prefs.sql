-- Migration v6: per-user complex visibility preferences
-- Mirrors user_recipe_prefs pattern for complexes.

CREATE TABLE IF NOT EXISTS user_complex_prefs (
    user_id    INT     NOT NULL REFERENCES users(id)    ON DELETE CASCADE,
    complex_id INT     NOT NULL REFERENCES complexes(id) ON DELETE CASCADE,
    hidden     BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (user_id, complex_id)
);

CREATE INDEX IF NOT EXISTS idx_user_complex_prefs_user ON user_complex_prefs (user_id);

DO $$ BEGIN RAISE NOTICE 'Migration v6 complete: user_complex_prefs added.'; END $$;
