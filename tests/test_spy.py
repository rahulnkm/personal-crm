from tests._spy import CountingClient


class _FakeBuilder:
    def __init__(self, sink, table):
        self._sink, self._table = sink, table

    def select(self, *a, **k):
        return self

    def update(self, *a, **k):
        self._op = "update"
        return self

    def insert(self, *a, **k):
        self._op = "insert"
        return self

    def in_(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def execute(self):
        self._sink.append((self._table, getattr(self, "_op", "select")))

        class R:
            data = []

        return R()


class _FakeClient:
    def __init__(self):
        self.calls = []

    def table(self, name):
        return _FakeBuilder(self.calls, name)

    def rpc(self, name, params):
        self.calls.append(("rpc", name))

        class B:
            def execute(self_inner):
                class R:
                    data = []

                return R()

        return B()


def test_counts_table_ops_and_rpc():
    spy = CountingClient(_FakeClient())
    spy.table("contacts").update({"a": 1}).in_("id", [1]).execute()
    spy.table("contacts").insert([{"a": 1}]).execute()
    spy.rpc("crm_stats", {}).execute()
    assert spy.count("contacts", "update") == 1
    assert spy.count("contacts", "insert") == 1
    assert spy.rpc_count("crm_stats") == 1
    assert spy.total() == 3
