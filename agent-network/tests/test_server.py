import http.client
import importlib.util
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer


SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"
SPEC = importlib.util.spec_from_file_location("agent_network_server", SERVER_PATH)
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


class RelevanceTests(unittest.TestCase):
    def test_delivery_offer_routes_to_payments_and_cards_not_automotive(self):
        state = server.initial_state()
        post = {
            "id": "post-1",
            "authorAgentId": "swiggy",
            "tags": ["food-delivery", "offers", "diwali", "payments", "upi"],
        }
        routed = server.route_post(state, post)
        recipients = {item["recipientAgentId"] for item in routed}
        self.assertIn("phonepe", recipients)
        self.assertIn("hdfc-bank", recipients)
        self.assertNotIn("bajaj-auto", recipients)

    def test_unrelated_post_does_not_notify_every_agent(self):
        state = server.initial_state()
        routed = server.route_post(state, {
            "id": "post-2",
            "authorAgentId": "bajaj-auto",
            "tags": ["automotive", "motorcycles"],
        })
        self.assertEqual([item["recipientAgentId"] for item in routed], [])

    def test_infers_normalized_tags(self):
        self.assertEqual(
            server.infer_tags("Diwali delivery offer with UPI cashback"),
            ["diwali", "food-delivery", "offers", "upi"],
        )

    def test_positive_feedback_increases_related_topic_score(self):
        state = server.initial_state()
        phonepe = next(agent for agent in state["agents"] if agent["id"] == "phonepe")
        before = server.calculate_relevance(["food-delivery"], phonepe)
        phonepe["feedback"] = {"relevant": {"food-delivery": 1}}
        after = server.calculate_relevance(["food-delivery"], phonepe)
        self.assertGreater(after, before)


class PersistenceTests(unittest.TestCase):
    def test_state_is_written_and_read_back(self):
        with tempfile.TemporaryDirectory() as directory:
            data_file = Path(directory) / "state.json"
            with patch.object(server, "DATA_FILE", data_file):
                initial = server.initial_state()
                server.write_state(initial)
                loaded = server.read_state()
            self.assertEqual(len(loaded["agents"]), 4)
            self.assertTrue(data_file.exists())


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_patch = patch.object(server, "DATA_FILE", Path(self.temp_dir.name) / "state.json")
        self.data_patch.start()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.data_patch.stop()
        self.temp_dir.cleanup()

    def request_json(self, method, path, body=None, token=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=3)
        headers = {}
        encoded = None
        if body is not None:
            encoded = json.dumps(body)
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        connection.request(method, path, body=encoded, headers=headers)
        response = connection.getresponse()
        result = json.loads(response.read())
        connection.close()
        return response.status, result

    def test_post_routes_to_relevant_agents_and_accepts_a_comment(self):
        status, created = self.request_json(
            "POST",
            "/api/posts",
            {
                "text": "5% off all deliveries on Diwali with PhonePe UPI cashback.",
                "tags": ["food-delivery", "offers", "diwali", "payments", "upi"],
            },
            "demo-swiggy-token",
        )
        self.assertEqual(status, 201)
        recipients = {item["agentId"] for item in created["routedTo"]}
        self.assertEqual(recipients, {"phonepe", "hdfc-bank"})
        post_id = created["post"]["id"]

        status, inbox = self.request_json(
            "GET", "/api/notifications", token="demo-phonepe-token"
        )
        self.assertEqual(status, 200)
        self.assertEqual(inbox["notifications"][0]["post"]["id"], post_id)

        status, comment = self.request_json(
            "POST",
            f"/api/posts/{post_id}/comments",
            {"text": "Pay with PhonePe UPI for extra cashback."},
            "demo-phonepe-token",
        )
        self.assertEqual(status, 201)
        self.assertEqual(comment["comment"]["authorName"], "PhonePe")

    def test_unverified_registered_agent_cannot_publish(self):
        status, registered = self.request_json(
            "POST",
            "/api/agents",
            {"name": "New Agent", "organization": "Example Co", "interests": ["offers"]},
        )
        self.assertEqual(status, 201)
        status, response = self.request_json(
            "POST",
            "/api/posts",
            {"text": "A new offer", "tags": ["offers"]},
            registered["token"],
        )
        self.assertEqual(status, 403)
        self.assertIn("verification", response["error"])

    def test_agent_can_update_interests(self):
        status, response = self.request_json(
            "PATCH",
            "/api/agents/me/interests",
            {"interests": ["mobility", "new offers"]},
            "demo-phonepe-token",
        )
        self.assertEqual(status, 200)
        self.assertEqual(response["agent"]["interests"], ["mobility", "new-offers"])


if __name__ == "__main__":
    unittest.main()
