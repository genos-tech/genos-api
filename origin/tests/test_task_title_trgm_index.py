"""MIP-1996: the task title search (`icontains` on TaskMaster.title) is served by a pg_trgm GIN index."""

from django.db import connection
from django.test import TestCase

from origin.models.task.task_models import TaskMaster

INDEX = "taskmaster_title_trgm_idx"


class TaskTitleTrigramIndexTests(TestCase):
    def test_extension_and_index_exist(self):
        with connection.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
            self.assertIsNotNone(cur.fetchone(), "pg_trgm extension missing")
            cur.execute(
                "SELECT indexdef FROM pg_indexes WHERE tablename = %s AND indexname = %s",
                [TaskMaster._meta.db_table, INDEX],
            )
            row = cur.fetchone()
        self.assertIsNotNone(row, "trigram index missing")
        self.assertIn("gin", row[0].lower())
        self.assertIn("gin_trgm_ops", row[0])

    def test_icontains_title_query_uses_the_index(self):
        qs = TaskMaster.objects.filter(title__icontains="invoice")
        sql, params = qs.query.sql_with_params()
        with connection.cursor() as cur:
            # A tiny empty table prefers a seq scan; force the planner to show what is possible.
            cur.execute("SET LOCAL enable_seqscan = off")
            cur.execute("EXPLAIN " + sql, params)
            plan = "\n".join(r[0] for r in cur.fetchall())
        self.assertIn(INDEX, plan)
