import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from integrations.crypto import app as server
from integrations.crypto.paper import view


class PaperViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "paper.sqlite3"
        self.ts = int(time.time()*1000)//14_400_000*14_400_000
        self.clock = patch("integrations.crypto.paper.time.time", return_value=(self.ts+60_000)/1000)
        self.clock.start()
        self.state = {"books": {v: {"funded": 1., "net": 1., "funding_coverage": .5} for v in ["event","equal"]},
            "last_ts": self.ts, "started": self.ts, "step_ms": 14_400_000, "hold": 84, "cost": .001,
            "n_closed_positions": 0, "last_signal": {"ts": self.ts, "n": 40, "n_positions": 2},
            "cohorts": [{"id": self.ts, "exit_ts": self.ts+84*14_400_000,
                "positions": [{"base": "BTC", "side": "long", "entry": 100., "event": .5, "equal": .5, "score": 1.},
                              {"base": "BTC", "side": "short", "entry": 100., "event": .5, "equal": .5, "score": -1.}]}]}
        with sqlite3.connect(self.db) as con:
            con.executescript("CREATE TABLE state(id INTEGER,payload TEXT);CREATE TABLE closed(id INTEGER,payload TEXT);CREATE TABLE health(id INTEGER,payload TEXT);CREATE TABLE decisions(ts INTEGER,generated_ms INTEGER,model_version TEXT,payload TEXT);")
            con.execute("INSERT INTO state VALUES(1,?)", (json.dumps(self.state),))
            con.execute("INSERT INTO health VALUES(1,?)", (json.dumps({"status": "ok"}),))
            con.execute("INSERT INTO decisions VALUES(?,?,?,?)", (self.ts,self.ts+1000,'test',json.dumps({'cohort':self.state['cohorts'][0]})))

    def tearDown(self):
        self.clock.stop()
        self.tmp.cleanup()

    def quotes(self, bases):
        return {"BTC": {"last": 110., "ts": self.ts+60_000, "chg24": 1.}}

    def test_opposite_cohorts_net_correctly_and_positions_both_visible(self):
        b = view(self.db, self.quotes)
        self.assertEqual((b["long"], b["short"]), (1, 1))
        self.assertEqual(len(b["positions"]), 2)
        self.assertAlmostEqual(b["equity_mtm"], 1-.002/84/240)
        self.assertAlmostEqual(sum(p["upnl_usd"] for p in b["positions"]), 0.)
        self.assertEqual(b["series"], [])
        self.assertNotIn("equal", b)
        self.assertNotIn("internal_report", b)
        self.assertNotIn("sharpe", b)

    def test_missing_quote_does_not_report_zero_pnl_as_valid(self):
        b = view(self.db, lambda bases: {})
        self.assertIsNone(b["equity_mtm"])
        self.assertIsNone(b["upnl_usd"])
        self.assertEqual(b["upnl_n"], 0)

    def test_stale_prices_are_not_silently_accepted(self):
        b = view(self.db, lambda bases: {"BTC": {"last": 110., "ts": self.ts-3_600_000}})
        self.assertIsNone(b["mtm_usd"])

    def test_api_keeps_internal_reports_private(self):
        client = TestClient(server.app)
        with patch.object(server, "PAPER_DB", self.db), patch.object(server.market, "quotes", self.quotes):
            response = client.get("/api/book/cnn")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["mode"], "cohort")
        for route in ["/api/internal_report", "/api/book/equal", "/api/book/event", "/work/paper_trade/internal_report.json"]:
            self.assertEqual(client.get(route).status_code, 404)

    def test_next_tick_in_first_twelve_minutes_is_current_boundary(self):
        with patch.object(server.time, "time", return_value=(self.ts+3*60_000)/1000):
            self.assertEqual(server._tick()["next_ms"], self.ts+12*60_000)

    def test_both_variants_use_fee_only_equity_and_own_weights(self):
        self.state['books']['event'].update(net=1.1,funded=.9)
        self.state['books']['equal'].update(net=1.2,funded=.8)
        # Deliberately different long/short weights to catch a hard-coded event key.
        self.state['cohorts'][0]['positions'][0]['equal']=.25
        self.state['cohorts'][0]['positions'][1]['equal']=.75
        with sqlite3.connect(self.db) as con:
            con.execute('UPDATE state SET payload=?',(json.dumps(self.state),))
        event=view(self.db,self.quotes)
        equal=view(self.db,self.quotes,variant='equal')
        self.assertEqual(event['equity'],1.1)
        self.assertEqual(equal['equity'],1.2)
        self.assertAlmostEqual(equal['equity_mtm'],1.2-.06-1.2*.002/84/240)
        self.assertEqual((event['research_sharpe'],equal['research_sharpe']),(3.87,3.53))
        client=TestClient(server.app)
        with patch.object(server,'PAPER_DB',self.db),patch.object(server.market,'quotes',self.quotes):
            self.assertEqual(client.get('/api/book/cnn_equal').json()['equity'],1.2)

    def test_history_includes_old_closed_entries_with_real_provenance_and_pagination(self):
        old=self.ts-90*14_400_000
        cohort={'id':old,'positions':[{'base':'OLD','side':'short','entry':10.,'score':-2.,'event':.2,'equal':.1}]}
        with sqlite3.connect(self.db) as con:
            con.execute('INSERT INTO decisions VALUES(?,?,?,?)',(old,self.ts,'test',json.dumps({'origin':'retrospective','cohort':cohort})))
        first=view(self.db,self.quotes,history_limit=2)
        last=view(self.db,self.quotes,history_limit=2,history_offset=2)
        self.assertEqual(first['fills_total'],3)
        self.assertEqual([p['base'] for p in last['fills']],['OLD'])
        self.assertEqual(last['fills'][0]['origin'],'retrospective')
        self.assertEqual(last['fills'][0]['generated_ms'],self.ts)
        self.assertEqual(first['fills'][0]['origin'],'forward')


if __name__ == "__main__":
    unittest.main()
