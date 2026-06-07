"""
Тесты для подписок на комплексы сообщества и shadow fork (ghost complexes).
"""
import pytest
from tests.conftest import make_complex_payload

RECIPE_ID = 9001


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _find_in_all_pages(c, url_base, predicate, type_filter="complex"):
    """Iterate through all /api/nodes pages to find a matching item."""
    sep = "&" if "?" in url_base else "?"
    page = 1
    while True:
        r = c.get(f"{url_base}{sep}type={type_filter}&page={page}&per_page=100")
        data = r.get_json()
        found = next((n for n in data["items"] if predicate(n)), None)
        if found is not None:
            return found
        if page >= data.get("pages", 1):
            return None
        page += 1

def _make_real_user(db_conn, tag):
    """Create a non-guest user with a unique tag; returns user_id."""
    email = f"test-{tag}@subscriptions-test.invalid"
    with db_conn.cursor() as cur:
        cur.execute("""
            INSERT INTO users (provider, provider_user_id, display_name, is_guest)
            VALUES ('email', %s, %s, FALSE)
            RETURNING id
        """, (email, f"Test {tag}"))
        return cur.fetchone()[0]


def _make_real_client(app, user_id):
    """Return a test client authenticated as the given user_id."""
    c = app.test_client()
    c.get("/")  # create a guest session first (needed for before_request logic)
    with c.session_transaction() as sess:
        sess["user_id"] = user_id
    return c


def _delete_user_and_data(db_conn, *user_ids):
    """Remove users and their directly owned complexes (cleanup helper)."""
    with db_conn.cursor() as cur:
        for uid in user_ids:
            cur.execute("SELECT id FROM complexes WHERE user_id = %s", (uid,))
            cids = [r[0] for r in cur.fetchall()]
            for cid in cids:
                _delete_complex_deep(cur, cid)
            cur.execute("DELETE FROM users WHERE id = %s", (uid,))


def _delete_complex_deep(cur, cid):
    """Delete a complex and all its sub-tables (no FK cascade in all cases)."""
    cur.execute("DELETE FROM complex_subscriptions WHERE complex_id = %s", (cid,))
    cur.execute("DELETE FROM complex_likes WHERE complex_id = %s", (cid,))
    cur.execute("DELETE FROM complex_edges WHERE complex_id = %s", (cid,))
    cur.execute("DELETE FROM complex_maintenance WHERE complex_id = %s", (cid,))
    cur.execute("DELETE FROM resource_flows WHERE complex_id = %s", (cid,))
    cur.execute("DELETE FROM complex_members WHERE complex_id = %s", (cid,))
    # Also remove any complex_members *in other complexes* that reference this cid
    cur.execute("DELETE FROM complex_members WHERE child_complex_id = %s", (cid,))
    cur.execute("DELETE FROM complexes WHERE id = %s", (cid,))


# ─── Subscribe / Unsubscribe endpoint ────────────────────────────────────────

class TestSubscribeEndpoint:
    """POST / DELETE /api/complex/<id>/subscribe"""

    def test_guest_cannot_subscribe(self, client, seed_public_complex):
        r = client.post(f"/api/complex/{seed_public_complex['id']}/subscribe")
        assert r.status_code == 401

    def test_guest_cannot_unsubscribe(self, client, seed_public_complex):
        r = client.delete(f"/api/complex/{seed_public_complex['id']}/subscribe")
        assert r.status_code == 401

    def test_real_user_can_subscribe(self, app, db_conn, seed_game_data, seed_public_complex):
        uid = _make_real_user(db_conn, "sub-ok")
        try:
            c = _make_real_client(app, uid)
            r = c.post(f"/api/complex/{seed_public_complex['id']}/subscribe")
            assert r.status_code == 200
            d = r.get_json()
            assert d["ok"] is True
            assert d["subscribed"] is True
        finally:
            with db_conn.cursor() as cur:
                cur.execute("DELETE FROM complex_subscriptions WHERE user_id = %s", (uid,))
                cur.execute("DELETE FROM users WHERE id = %s", (uid,))

    def test_cannot_subscribe_to_own_complex(self, app, db_conn, seed_game_data):
        uid = _make_real_user(db_conn, "sub-own")
        with db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO complexes (name, user_id, visibility)
                VALUES ('Own Public Sub Test', %s, 'public') RETURNING id
            """, (uid,))
            cid = cur.fetchone()[0]
        try:
            c = _make_real_client(app, uid)
            r = c.post(f"/api/complex/{cid}/subscribe")
            assert r.status_code == 400
            assert "cannot subscribe" in r.get_json().get("error", "")
        finally:
            with db_conn.cursor() as cur:
                cur.execute("DELETE FROM complexes WHERE id = %s", (cid,))
                cur.execute("DELETE FROM users WHERE id = %s", (uid,))

    def test_subscribe_to_private_returns_404(self, app, db_conn, seed_game_data):
        uid   = _make_real_user(db_conn, "sub-priv-user")
        owner = _make_real_user(db_conn, "sub-priv-owner")
        with db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO complexes (name, user_id, visibility)
                VALUES ('Private For Sub Test', %s, 'private') RETURNING id
            """, (owner,))
            cid = cur.fetchone()[0]
        try:
            c = _make_real_client(app, uid)
            r = c.post(f"/api/complex/{cid}/subscribe")
            assert r.status_code == 404
        finally:
            with db_conn.cursor() as cur:
                cur.execute("DELETE FROM complexes WHERE id = %s", (cid,))
                cur.execute("DELETE FROM users WHERE id IN (%s, %s)", (uid, owner))

    def test_subscribe_idempotent(self, app, db_conn, seed_game_data, seed_public_complex):
        uid = _make_real_user(db_conn, "sub-idem")
        try:
            c  = _make_real_client(app, uid)
            cid = seed_public_complex["id"]
            r1 = c.post(f"/api/complex/{cid}/subscribe")
            r2 = c.post(f"/api/complex/{cid}/subscribe")
            assert r1.status_code == 200
            assert r2.status_code == 200
        finally:
            with db_conn.cursor() as cur:
                cur.execute("DELETE FROM complex_subscriptions WHERE user_id = %s", (uid,))
                cur.execute("DELETE FROM users WHERE id = %s", (uid,))

    def test_unsubscribe(self, app, db_conn, seed_game_data, seed_public_complex):
        uid = _make_real_user(db_conn, "sub-del")
        try:
            c   = _make_real_client(app, uid)
            cid = seed_public_complex["id"]
            c.post(f"/api/complex/{cid}/subscribe")
            r = c.delete(f"/api/complex/{cid}/subscribe")
            assert r.status_code == 200
            assert r.get_json()["subscribed"] is False
            # Row removed from DB
            with db_conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM complex_subscriptions WHERE user_id=%s AND complex_id=%s",
                    (uid, cid)
                )
                assert cur.fetchone() is None
        finally:
            with db_conn.cursor() as cur:
                cur.execute("DELETE FROM complex_subscriptions WHERE user_id = %s", (uid,))
                cur.execute("DELETE FROM users WHERE id = %s", (uid,))


# ─── _subscribed field in API responses ──────────────────────────────────────

class TestSubscribedField:
    """_subscribed=True/False in /api/complexes/public and /api/nodes."""

    def test_public_listing_subscribed_false_for_guest(self, client, seed_public_complex):
        r = client.get("/api/complexes/public")
        items = r.get_json()["items"]
        found = next((x for x in items if x["id"] == seed_public_complex["id"]), None)
        assert found is not None
        assert not found.get("_subscribed")

    def test_public_listing_subscribed_true_after_subscribe(
        self, app, db_conn, seed_game_data, seed_public_complex
    ):
        uid = _make_real_user(db_conn, "sub-field-pub")
        try:
            c   = _make_real_client(app, uid)
            cid = seed_public_complex["id"]
            c.post(f"/api/complex/{cid}/subscribe")
            r = c.get("/api/complexes/public")
            items = r.get_json()["items"]
            found = next((x for x in items if x["id"] == cid), None)
            assert found is not None
            assert found.get("_subscribed") is True
        finally:
            with db_conn.cursor() as cur:
                cur.execute("DELETE FROM complex_subscriptions WHERE user_id = %s", (uid,))
                cur.execute("DELETE FROM users WHERE id = %s", (uid,))

    def test_nodes_api_subscribed_complex_appears_with_flag(
        self, app, db_conn, seed_game_data, seed_public_complex
    ):
        """After subscribing, the community complex appears in /api/nodes with _subscribed=True."""
        uid = _make_real_user(db_conn, "sub-browse")
        try:
            c   = _make_real_client(app, uid)
            cid = seed_public_complex["id"]
            c.post(f"/api/complex/{cid}/subscribe")
            found = _find_in_all_pages(c, "/api/nodes", lambda n: n.get("node_id") == cid)
            assert found is not None, "Subscribed complex must appear in Browse"
            assert found.get("_subscribed") is True
        finally:
            with db_conn.cursor() as cur:
                cur.execute("DELETE FROM complex_subscriptions WHERE user_id = %s", (uid,))
                cur.execute("DELETE FROM users WHERE id = %s", (uid,))

    def test_own_complex_not_subscribed_in_browse(self, app, db_conn, seed_game_data):
        """User's own complex appears in Browse but _subscribed is False."""
        uid = _make_real_user(db_conn, "own-browse")
        with db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO complexes (name, user_id, visibility)
                VALUES ('Own Browse Test', %s, 'private') RETURNING id
            """, (uid,))
            cid = cur.fetchone()[0]
        try:
            c = _make_real_client(app, uid)
            found = _find_in_all_pages(c, "/api/nodes", lambda n: n.get("node_id") == cid)
            assert found is not None, "Own complex must appear in Browse"
            assert not found.get("_subscribed")
        finally:
            with db_conn.cursor() as cur:
                _delete_complex_deep(cur, cid)
                cur.execute("DELETE FROM users WHERE id = %s", (uid,))


# ─── Shadow Fork ──────────────────────────────────────────────────────────────

class TestShadowFork:
    """Ghost copy is created when an owner edits/deletes/privatizes a community complex
    that other users' complexes depend on."""

    def _setup(self, db_conn):
        """Create owner (A) with a public complex, and user B whose complex uses it."""
        with db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (provider, provider_user_id, display_name, is_guest)
                VALUES ('email', 'ghost-owner-a@test.invalid', 'Ghost Owner A', FALSE)
                RETURNING id
            """)
            a_uid = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO complexes (name, user_id, visibility)
                VALUES ('Community Complex Ghost', %s, 'public') RETURNING id
            """, (a_uid,))
            a_cid = cur.fetchone()[0]

            cur.execute("""
                INSERT INTO users (provider, provider_user_id, display_name, is_guest)
                VALUES ('email', 'ghost-user-b@test.invalid', 'Ghost User B', FALSE)
                RETURNING id
            """)
            b_uid = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO complexes (name, user_id, visibility)
                VALUES ('B Uses Ghost Complex', %s, 'private') RETURNING id
            """, (b_uid,))
            b_cid = cur.fetchone()[0]
            # B's complex uses A's complex as a member node
            cur.execute("""
                INSERT INTO complex_members
                    (complex_id, child_type, child_id, child_complex_id, multiplier, pos_x, pos_y)
                VALUES (%s, 1, %s, %s, 1, 100, 100)
            """, (b_cid, a_cid, a_cid))

        return {"a_uid": a_uid, "a_cid": a_cid, "b_uid": b_uid, "b_cid": b_cid}

    def _teardown(self, db_conn, ids, a_deleted=False):
        """Clean up all test data, including any ghosts created."""
        with db_conn.cursor() as cur:
            # Find and delete any ghosts that were created
            cur.execute("""
                SELECT id FROM complexes
                WHERE is_ghost = TRUE AND (ghost_of_id = %s OR ghost_of_id IS NULL)
                  AND name = 'Community Complex Ghost'
            """, (ids["a_cid"],))
            ghost_ids = [r[0] for r in cur.fetchall()]
            for gid in ghost_ids:
                _delete_complex_deep(cur, gid)

            # Delete B's complex (members may now point to ghost, already deleted above)
            cur.execute("DELETE FROM complex_edges WHERE complex_id = %s", (ids["b_cid"],))
            cur.execute("DELETE FROM resource_flows WHERE complex_id = %s", (ids["b_cid"],))
            cur.execute("DELETE FROM complex_members WHERE complex_id = %s", (ids["b_cid"],))
            cur.execute("DELETE FROM complexes WHERE id = %s", (ids["b_cid"],))

            # Delete A's complex (if not already deleted by test)
            if not a_deleted:
                _delete_complex_deep(cur, ids["a_cid"])

            cur.execute("DELETE FROM users WHERE id IN (%s, %s)", (ids["a_uid"], ids["b_uid"]))

    def test_ghost_created_on_privatize(self, app, db_conn, seed_game_data):
        """Privatizing a used public complex creates a ghost and re-links B's member."""
        ids = self._setup(db_conn)
        try:
            c = _make_real_client(app, ids["a_uid"])
            r = c.patch(
                f"/api/complex/{ids['a_cid']}/visibility",
                json={"visibility": "private"},
            )
            assert r.status_code == 200

            with db_conn.cursor() as cur:
                cur.execute("""
                    SELECT id, ghost_reason FROM complexes
                    WHERE is_ghost = TRUE AND ghost_of_id = %s
                """, (ids["a_cid"],))
                ghost_row = cur.fetchone()
            assert ghost_row is not None, "Ghost must be created when privatizing a used complex"
            assert ghost_row[1] == "privatized"
            ghost_id = ghost_row[0]

            # B's complex_member should now point to the ghost
            with db_conn.cursor() as cur:
                cur.execute("""
                    SELECT child_complex_id FROM complex_members WHERE complex_id = %s
                """, (ids["b_cid"],))
                row = cur.fetchone()
            assert row is not None
            assert row[0] == ghost_id, "B's member must be redirected to ghost"
        finally:
            self._teardown(db_conn, ids)

    def test_ghost_created_on_delete(self, app, db_conn, seed_game_data):
        """Deleting a used public complex creates a ghost before removal."""
        ids = self._setup(db_conn)
        # Find the max ghost id before deletion to identify newly created ghost
        with db_conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(id), 0) FROM complexes WHERE is_ghost = TRUE")
            max_before = cur.fetchone()[0]
        try:
            c = _make_real_client(app, ids["a_uid"])
            r = c.delete(f"/api/complex/{ids['a_cid']}")
            assert r.status_code == 200

            with db_conn.cursor() as cur:
                cur.execute("""
                    SELECT id, ghost_reason FROM complexes
                    WHERE is_ghost = TRUE AND id > %s AND name = 'Community Complex Ghost'
                """, (max_before,))
                ghost_row = cur.fetchone()
            assert ghost_row is not None, "Ghost must be created before deleting a used complex"
            assert ghost_row[1] == "deleted"
            ghost_id = ghost_row[0]

            # B's member should now point to the ghost
            with db_conn.cursor() as cur:
                cur.execute("""
                    SELECT child_complex_id FROM complex_members WHERE complex_id = %s
                """, (ids["b_cid"],))
                row = cur.fetchone()
            assert row is not None
            assert row[0] == ghost_id
        finally:
            self._teardown(db_conn, ids, a_deleted=True)

    def test_no_ghost_when_no_dependents(self, app, db_conn, seed_game_data):
        """No ghost is created when no one else depends on the complex."""
        uid = _make_real_user(db_conn, "no-ghost-solo")
        with db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO complexes (name, user_id, visibility)
                VALUES ('Solo Public No Ghost', %s, 'public') RETURNING id
            """, (uid,))
            cid = cur.fetchone()[0]
        try:
            c = _make_real_client(app, uid)
            r = c.patch(
                f"/api/complex/{cid}/visibility",
                json={"visibility": "private"},
            )
            assert r.status_code == 200

            with db_conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM complexes WHERE is_ghost = TRUE AND ghost_of_id = %s",
                    (cid,),
                )
                count = cur.fetchone()[0]
            assert count == 0, "No ghost should be created when nobody depends on this complex"
        finally:
            with db_conn.cursor() as cur:
                _delete_complex_deep(cur, cid)
                cur.execute("DELETE FROM users WHERE id = %s", (uid,))

    def test_ghost_not_in_nodes_api(self, app, db_conn):
        """Ghost complexes do not appear in /api/nodes (Browse tab)."""
        with db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO complexes (name, user_id, visibility, is_ghost, ghost_reason)
                VALUES ('__ghost_browse_test__', NULL, 'public', TRUE, 'deleted')
                RETURNING id
            """)
            ghost_id = cur.fetchone()[0]
        try:
            c = app.test_client()
            c.get("/")
            r = c.get("/api/nodes?per_page=500")
            node_ids = [n.get("node_id") for n in r.get_json()["items"]]
            assert ghost_id not in node_ids, "Ghost must not appear in Browse"
        finally:
            with db_conn.cursor() as cur:
                cur.execute("DELETE FROM complexes WHERE id = %s", (ghost_id,))

    def test_ghost_not_in_public_complexes_api(self, app, db_conn):
        """Ghost complexes do not appear in /api/complexes/public."""
        with db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO complexes (name, user_id, visibility, is_ghost, ghost_reason)
                VALUES ('__ghost_pub_test__', NULL, 'public', TRUE, 'edited')
                RETURNING id
            """)
            ghost_id = cur.fetchone()[0]
        try:
            c = app.test_client()
            c.get("/")
            r = c.get("/api/complexes/public")
            ids = [x["id"] for x in r.get_json()["items"]]
            assert ghost_id not in ids, "Ghost must not appear in community listing"
        finally:
            with db_conn.cursor() as cur:
                cur.execute("DELETE FROM complexes WHERE id = %s", (ghost_id,))


# ─── Auto-subscribe ───────────────────────────────────────────────────────────

class TestAutoSubscribe:
    """Auto-subscription to community nodes when saving a complex."""

    def test_auto_subscribe_on_create(self, app, db_conn, seed_game_data, seed_public_complex):
        """Creating a complex that contains a community node auto-subscribes to it."""
        uid = _make_real_user(db_conn, "auto-sub-create")
        cid = None
        try:
            c = _make_real_client(app, uid)
            payload = {
                "name": "Auto Sub Create Test",
                "nodes": [
                    {
                        "_id":         "node-1",
                        "node_type":   "complex",
                        "node_ref_id": seed_public_complex["id"],
                        "count":       1,
                        "pos_x":       100,
                        "pos_y":       100,
                        "efficiency":  1.0,
                    }
                ],
                "edges": [],
            }
            r = c.post("/api/complex", json=payload)
            assert r.status_code == 201
            cid = r.get_json()["id"]

            with db_conn.cursor() as cur:
                cur.execute("""
                    SELECT 1 FROM complex_subscriptions
                    WHERE user_id = %s AND complex_id = %s
                """, (uid, seed_public_complex["id"]))
                row = cur.fetchone()
            assert row is not None, "User should be auto-subscribed to used community complex"
        finally:
            with db_conn.cursor() as cur:
                cur.execute("DELETE FROM complex_subscriptions WHERE user_id = %s", (uid,))
                if cid:
                    _delete_complex_deep(cur, cid)
                cur.execute("DELETE FROM users WHERE id = %s", (uid,))

    def test_auto_subscribe_skips_own_complex(self, app, db_conn, seed_game_data):
        """Creating a complex that contains own community node does NOT auto-subscribe."""
        uid = _make_real_user(db_conn, "auto-sub-own")
        pub_cid  = None
        priv_cid = None
        try:
            # User publishes a complex directly in DB
            with db_conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO complexes (name, user_id, visibility)
                    VALUES ('My Own Public AutoSub', %s, 'public') RETURNING id
                """, (uid,))
                pub_cid = cur.fetchone()[0]

            c = _make_real_client(app, uid)
            payload = {
                "name": "Auto Sub Own Test",
                "nodes": [
                    {
                        "_id":         "node-1",
                        "node_type":   "complex",
                        "node_ref_id": pub_cid,
                        "count":       1,
                        "pos_x":       100,
                        "pos_y":       100,
                        "efficiency":  1.0,
                    }
                ],
                "edges": [],
            }
            r = c.post("/api/complex", json=payload)
            assert r.status_code == 201
            priv_cid = r.get_json()["id"]

            with db_conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) FROM complex_subscriptions
                    WHERE user_id = %s AND complex_id = %s
                """, (uid, pub_cid))
                count = cur.fetchone()[0]
            assert count == 0, "Should not auto-subscribe user to their own complex"
        finally:
            with db_conn.cursor() as cur:
                cur.execute("DELETE FROM complex_subscriptions WHERE user_id = %s", (uid,))
                if priv_cid:
                    _delete_complex_deep(cur, priv_cid)
                if pub_cid:
                    _delete_complex_deep(cur, pub_cid)
                cur.execute("DELETE FROM users WHERE id = %s", (uid,))


# ─── cleanup-ghosts CLI ───────────────────────────────────────────────────────

class TestCleanupGhosts:
    """Flask CLI command: flask cleanup-ghosts."""

    def test_cleanup_removes_orphaned_ghost(self, app, db_conn):
        """A ghost with no complex_members referencing it is deleted."""
        with db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO complexes (name, user_id, visibility, is_ghost, ghost_reason)
                VALUES ('__orphan_ghost_cleanup__', NULL, 'public', TRUE, 'deleted')
                RETURNING id
            """)
            ghost_id = cur.fetchone()[0]
        try:
            runner = app.test_cli_runner()
            result = runner.invoke(args=["cleanup-ghosts"])
            assert result.exit_code == 0, f"CLI error: {result.output}"

            with db_conn.cursor() as cur:
                cur.execute("SELECT id FROM complexes WHERE id = %s", (ghost_id,))
                row = cur.fetchone()
            assert row is None, "Orphaned ghost should be removed by cleanup-ghosts"
        finally:
            # Safeguard: clean up if CLI didn't delete it
            with db_conn.cursor() as cur:
                cur.execute("DELETE FROM complexes WHERE id = %s", (ghost_id,))

    def test_cleanup_keeps_referenced_ghost(self, app, db_conn, seed_game_data):
        """A ghost that is still used by a complex_member must NOT be deleted."""
        uid = _make_real_user(db_conn, "cleanup-ref")
        ghost_id  = None
        parent_id = None
        try:
            with db_conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO complexes (name, user_id, visibility, is_ghost, ghost_reason)
                    VALUES ('__referenced_ghost__', NULL, 'public', TRUE, 'edited')
                    RETURNING id
                """)
                ghost_id = cur.fetchone()[0]
                cur.execute("""
                    INSERT INTO complexes (name, user_id, visibility)
                    VALUES ('Uses Referenced Ghost', %s, 'private') RETURNING id
                """, (uid,))
                parent_id = cur.fetchone()[0]
                cur.execute("""
                    INSERT INTO complex_members
                        (complex_id, child_type, child_id, child_complex_id, multiplier, pos_x, pos_y)
                    VALUES (%s, 1, %s, %s, 1, 100, 100)
                """, (parent_id, ghost_id, ghost_id))

            runner = app.test_cli_runner()
            result = runner.invoke(args=["cleanup-ghosts"])
            assert result.exit_code == 0, f"CLI error: {result.output}"

            with db_conn.cursor() as cur:
                cur.execute("SELECT id FROM complexes WHERE id = %s", (ghost_id,))
                row = cur.fetchone()
            assert row is not None, "Referenced ghost must NOT be deleted"
        finally:
            with db_conn.cursor() as cur:
                if parent_id:
                    cur.execute("DELETE FROM complex_members WHERE complex_id = %s", (parent_id,))
                    cur.execute("DELETE FROM complexes WHERE id = %s", (parent_id,))
                if ghost_id:
                    cur.execute("DELETE FROM complexes WHERE id = %s", (ghost_id,))
                cur.execute("DELETE FROM users WHERE id = %s", (uid,))

    def test_cleanup_dry_run_does_not_delete(self, app, db_conn):
        """--dry-run flag reports but does not delete orphaned ghosts."""
        with db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO complexes (name, user_id, visibility, is_ghost, ghost_reason)
                VALUES ('__dry_run_ghost__', NULL, 'public', TRUE, 'privatized')
                RETURNING id
            """)
            ghost_id = cur.fetchone()[0]
        try:
            runner = app.test_cli_runner()
            result = runner.invoke(args=["cleanup-ghosts", "--dry-run"])
            assert result.exit_code == 0, f"CLI error: {result.output}"

            with db_conn.cursor() as cur:
                cur.execute("SELECT id FROM complexes WHERE id = %s", (ghost_id,))
                row = cur.fetchone()
            assert row is not None, "Dry-run must not actually delete the ghost"
        finally:
            with db_conn.cursor() as cur:
                cur.execute("DELETE FROM complexes WHERE id = %s", (ghost_id,))
