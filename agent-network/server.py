#!/usr/bin/env python3
"""Local demo of an authenticated, relevance-routed agent social network."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
DATA_FILE = ROOT / "data" / "state.json"
LOCK = threading.RLock()
MAX_BODY_BYTES = 64 * 1024
MIN_RELEVANCE_SCORE = 0.28

DEMO_AGENTS = (
    {
        "id": "swiggy",
        "name": "Swiggy",
        "organization": "Swiggy",
        "description": "Food delivery offers and service announcements",
        "interests": ["food-delivery", "offers", "restaurants"],
        "demoToken": "demo-swiggy-token",
    },
    {
        "id": "phonepe",
        "name": "PhonePe",
        "organization": "PhonePe",
        "description": "Payments, UPI and partner offers",
        "interests": ["payments", "upi", "offers", "diwali"],
        "demoToken": "demo-phonepe-token",
    },
    {
        "id": "hdfc-bank",
        "name": "HDFC Bank",
        "organization": "HDFC Bank",
        "description": "Cards, banking and customer offers",
        "interests": ["cards", "payments", "offers", "diwali"],
        "demoToken": "demo-hdfc-token",
    },
    {
        "id": "bajaj-auto",
        "name": "Bajaj Auto",
        "organization": "Bajaj Auto",
        "description": "Automotive, motorcycles and mobility",
        "interests": ["automotive", "motorcycles", "mobility"],
        "demoToken": "demo-bajaj-token",
    },
)

KEYWORD_TAGS = {
    "swiggy": "food-delivery",
    "delivery": "food-delivery",
    "restaurant": "food-delivery",
    "restaurants": "food-delivery",
    "food": "food-delivery",
    "upi": "upi",
    "phonepe": "payments",
    "payment": "payments",
    "payments": "payments",
    "cashback": "offers",
    "offer": "offers",
    "offers": "offers",
    "discount": "offers",
    "discounts": "offers",
    "card": "cards",
    "cards": "cards",
    "bank": "banking",
    "banking": "banking",
    "diwali": "diwali",
    "motorcycle": "motorcycles",
    "motorcycles": "motorcycles",
    "automotive": "automotive",
    "vehicle": "automotive",
    "vehicles": "automotive",
    "mobility": "mobility",
}

TAG_AFFINITIES = {
    "food-delivery": {"payments": 0.45, "upi": 0.35, "cards": 0.30},
    "offers": {"payments": 0.25, "upi": 0.25, "cards": 0.25},
    "diwali": {"payments": 0.15, "cards": 0.15},
    "payments": {"food-delivery": 0.25, "offers": 0.25},
    "cards": {"offers": 0.20},
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalized_tags(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("interests and tags must be arrays of strings")
    tags = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("each tag must be a non-empty string")
        tag = re.sub(r"[^a-z0-9-]", "-", item.strip().lower())
        tag = re.sub(r"-+", "-", tag).strip("-")
        if tag and tag not in tags:
            tags.append(tag)
    if len(tags) > 30:
        raise ValueError("at most 30 tags are allowed")
    return tags


def infer_tags(text: str) -> list[str]:
    found = []
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        tag = KEYWORD_TAGS.get(word)
        if tag and tag not in found:
            found.append(tag)
    return found


def initial_state() -> dict:
    agents = []
    for entry in DEMO_AGENTS:
        agent = {key: value for key, value in entry.items() if key != "demoToken"}
        agent["tokenHash"] = token_hash(entry["demoToken"])
        agent["verified"] = True
        agent["createdAt"] = now_iso()
        agent["feedback"] = {}
        agents.append(agent)
    return {"agents": agents, "posts": [], "notifications": [], "comments": []}


def read_state() -> dict:
    with LOCK:
        if not DATA_FILE.exists():
            return initial_state()
        try:
            content = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Unable to read network data: {error}") from error
        if not isinstance(content, dict) or not all(
            isinstance(content.get(key), list)
            for key in ("agents", "posts", "notifications", "comments")
        ):
            raise RuntimeError("Network data file has an invalid structure")
        return content


def write_state(state: dict) -> None:
    with LOCK:
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = DATA_FILE.with_suffix(".tmp")
        try:
            temporary.write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(temporary, DATA_FILE)
        except OSError as error:
            raise RuntimeError(f"Unable to save network data: {error}") from error


def public_agent(agent: dict) -> dict:
    return {key: value for key, value in agent.items() if key not in ("tokenHash", "feedback")}


def calculate_relevance(post_tags: list[str], agent: dict) -> float:
    interests = set(agent["interests"])
    if not post_tags:
        return 0.0
    relevant = agent.get("feedback", {}).get("relevant", {})
    not_relevant = agent.get("feedback", {}).get("not-relevant", {})
    score = 0.0
    for tag in post_tags:
        if tag in interests:
            tag_score = 1.0
        else:
            tag_score = max(
            (weight for interest in interests
             for weight in [TAG_AFFINITIES.get(tag, {}).get(interest, 0.0)]),
            default=0.0,
        )
        tag_score += min(0.15, relevant.get(tag, 0) * 0.02)
        tag_score -= min(0.25, not_relevant.get(tag, 0) * 0.04)
        score += max(0.0, tag_score)
    return min(1.0, score / len(post_tags))


def route_post(state: dict, post: dict) -> list[dict]:
    routed = []
    for agent in state["agents"]:
        if agent["id"] == post["authorAgentId"] or not agent.get("verified"):
            continue
        score = calculate_relevance(post["tags"], agent)
        if score < MIN_RELEVANCE_SCORE:
            continue
        notification = {
            "id": str(uuid.uuid4()),
            "postId": post["id"],
            "recipientAgentId": agent["id"],
            "relevanceScore": round(score, 3),
            "status": "pending",
            "createdAt": now_iso(),
        }
        state["notifications"].append(notification)
        routed.append(notification)
    return routed


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Handler(BaseHTTPRequestHandler):
    server_version = "AgentNetwork/1.0"

    def log_message(self, format_string: str, *args: object) -> None:
        print(f"{self.address_string()} - {format_string % args}")

    def send_json(self, status: int, payload: object) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ApiError(400, "Invalid Content-Length") from error
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ApiError(413 if length > MAX_BODY_BYTES else 400, "Invalid request body size")
        try:
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ApiError(400, "Body must be valid JSON") from error
        if not isinstance(body, dict):
            raise ApiError(400, "Body must be a JSON object")
        return body

    def authenticated_agent(self, state: dict) -> dict:
        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            raise ApiError(401, "Provide an agent token using Authorization: Bearer <token>")
        supplied_hash = token_hash(authorization[7:].strip())
        for agent in state["agents"]:
            if secrets.compare_digest(agent["tokenHash"], supplied_hash):
                return agent
        raise ApiError(401, "Invalid agent token")

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            state = read_state()
            if parsed.path == "/api/health":
                self.send_json(200, {"ok": True, "service": "agent-network"})
                return
            if parsed.path == "/api/agents":
                self.send_json(200, {"agents": [public_agent(agent) for agent in state["agents"]]})
                return
            if parsed.path == "/api/posts":
                posts = sorted(state["posts"], key=lambda post: post["createdAt"], reverse=True)
                self.send_json(200, {"posts": posts[:100]})
                return
            match = re.fullmatch(r"/api/posts/([a-f0-9-]+)/comments", parsed.path)
            if match:
                post_id = match.group(1)
                self.send_json(200, {"comments": [
                    comment for comment in state["comments"] if comment["postId"] == post_id
                ]})
                return
            if parsed.path == "/api/notifications":
                agent = self.authenticated_agent(state)
                query = parse_qs(parsed.query)
                status_filter = query.get("status", [""])[0]
                notifications = [
                    {**notification, "post": self.find_post(state, notification["postId"])}
                    for notification in state["notifications"]
                    if notification["recipientAgentId"] == agent["id"]
                    and (not status_filter or notification["status"] == status_filter)
                ]
                notifications.sort(key=lambda item: item["createdAt"], reverse=True)
                self.send_json(200, {"notifications": notifications[:100]})
                return
            if parsed.path == "/":
                self.serve_file("index.html")
                return
            if parsed.path.startswith("/web/"):
                self.serve_file(parsed.path.removeprefix("/web/"))
                return
            raise ApiError(404, "Route not found")
        except ApiError as error:
            self.send_json(error.status, {"error": str(error)})
        except Exception as error:
            print(f"Request failed: {error}")
            self.send_json(500, {"error": "Internal server error"})

    def do_POST(self) -> None:
        try:
            path = urlparse(self.path).path
            body = self.read_json()
            with LOCK:
                state = read_state()
                if path == "/api/agents":
                    self.register_agent(state, body)
                    return
                agent = self.authenticated_agent(state)
                if path == "/api/posts":
                    self.create_post(state, agent, body)
                    return
                match = re.fullmatch(r"/api/posts/([a-f0-9-]+)/comments", path)
                if match:
                    self.create_comment(state, agent, match.group(1), body)
                    return
                match = re.fullmatch(r"/api/notifications/([a-f0-9-]+)/feedback", path)
                if match:
                    self.save_feedback(state, agent, match.group(1), body)
                    return
                raise ApiError(404, "Route not found")
        except ApiError as error:
            self.send_json(error.status, {"error": str(error)})
        except Exception as error:
            print(f"Request failed: {error}")
            self.send_json(500, {"error": "Internal server error"})

    def do_PATCH(self) -> None:
        try:
            path = urlparse(self.path).path
            body = self.read_json()
            if path != "/api/agents/me/interests":
                raise ApiError(404, "Route not found")
            with LOCK:
                state = read_state()
                agent = self.authenticated_agent(state)
                try:
                    agent["interests"] = normalized_tags(body.get("interests", []))
                except ValueError as error:
                    raise ApiError(400, str(error)) from error
                write_state(state)
            self.send_json(200, {"agent": public_agent(agent)})
        except ApiError as error:
            self.send_json(error.status, {"error": str(error)})
        except Exception as error:
            print(f"Request failed: {error}")
            self.send_json(500, {"error": "Internal server error"})

    def register_agent(self, state: dict, body: dict) -> None:
        name = body.get("name")
        organization = body.get("organization")
        description = body.get("description", "")
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ApiError(400, "name is required and must be at most 80 characters")
        if not isinstance(organization, str) or not organization.strip() or len(organization) > 100:
            raise ApiError(400, "organization is required and must be at most 100 characters")
        if not isinstance(description, str) or len(description) > 500:
            raise ApiError(400, "description must be at most 500 characters")
        try:
            interests = normalized_tags(body.get("interests", []))
        except ValueError as error:
            raise ApiError(400, str(error)) from error
        token = secrets.token_urlsafe(32)
        agent = {
            "id": str(uuid.uuid4()),
            "name": name.strip(),
            "organization": organization.strip(),
            "description": description.strip(),
            "interests": interests,
            "tokenHash": token_hash(token),
            "verified": False,
            "createdAt": now_iso(),
            "feedback": {},
        }
        state["agents"].append(agent)
        write_state(state)
        self.send_json(201, {"agent": public_agent(agent), "token": token})

    def create_post(self, state: dict, agent: dict, body: dict) -> None:
        if not agent.get("verified"):
            raise ApiError(403, "This agent is pending organization verification and cannot publish yet")
        recent_posts = [
            post for post in state["posts"]
            if post["authorAgentId"] == agent["id"]
            and (datetime.now(timezone.utc) - datetime.fromisoformat(post["createdAt"])).total_seconds() < 3600
        ]
        if len(recent_posts) >= 20:
            raise ApiError(429, "Post limit reached. Try again later.")
        text = body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ApiError(400, "text is required and must be at most 2000 characters")
        supplied_tags = body.get("tags", [])
        if supplied_tags is None:
            supplied_tags = []
        try:
            tags = normalized_tags(supplied_tags)
        except ValueError as error:
            raise ApiError(400, str(error)) from error
        tags = list(dict.fromkeys(tags + infer_tags(text)))
        if not tags:
            raise ApiError(400, "Add at least one tag or mention a supported topic")
        post = {
            "id": str(uuid.uuid4()),
            "authorAgentId": agent["id"],
            "authorName": agent["name"],
            "text": text.strip(),
            "tags": tags,
            "createdAt": now_iso(),
        }
        state["posts"].append(post)
        routed = route_post(state, post)
        write_state(state)
        self.send_json(201, {"post": post, "routedTo": [
            {"agentId": item["recipientAgentId"], "relevanceScore": item["relevanceScore"]}
            for item in routed
        ]})

    def create_comment(self, state: dict, agent: dict, post_id: str, body: dict) -> None:
        if not agent.get("verified"):
            raise ApiError(403, "This agent is pending organization verification and cannot comment yet")
        if not any(post["id"] == post_id for post in state["posts"]):
            raise ApiError(404, "Post not found")
        recent_comments = [
            comment for comment in state["comments"]
            if comment["authorAgentId"] == agent["id"]
            and (datetime.now(timezone.utc) - datetime.fromisoformat(comment["createdAt"])).total_seconds() < 3600
        ]
        if len(recent_comments) >= 40:
            raise ApiError(429, "Comment limit reached. Try again later.")
        text = body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ApiError(400, "text is required and must be at most 2000 characters")
        comment = {
            "id": str(uuid.uuid4()),
            "postId": post_id,
            "authorAgentId": agent["id"],
            "authorName": agent["name"],
            "text": text.strip(),
            "createdAt": now_iso(),
        }
        state["comments"].append(comment)
        write_state(state)
        self.send_json(201, {"comment": comment})

    def save_feedback(self, state: dict, agent: dict, notification_id: str, body: dict) -> None:
        value = body.get("value")
        if value not in ("relevant", "not-relevant"):
            raise ApiError(400, "value must be 'relevant' or 'not-relevant'")
        notification = next(
            (item for item in state["notifications"] if item["id"] == notification_id), None
        )
        if notification is None or notification["recipientAgentId"] != agent["id"]:
            raise ApiError(404, "Notification not found")
        notification["status"] = "feedback"
        notification["feedback"] = value
        post = self.find_post(state, notification["postId"])
        counts = agent["feedback"].setdefault(value, {})
        for tag in post["tags"]:
            counts[tag] = counts.get(tag, 0) + 1
        write_state(state)
        self.send_json(200, {"notification": notification})

    @staticmethod
    def find_post(state: dict, post_id: str) -> dict | None:
        return next((post for post in state["posts"] if post["id"] == post_id), None)

    def serve_file(self, relative_path: str) -> None:
        requested = (WEB_ROOT / relative_path).resolve()
        if not requested.is_relative_to(WEB_ROOT.resolve()) or not requested.is_file():
            raise ApiError(404, "File not found")
        content = requested.read_bytes()
        content_type = "text/html; charset=utf-8" if requested.suffix == ".html" else (
            "text/javascript; charset=utf-8" if requested.suffix == ".js" else "text/css; charset=utf-8"
        )
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def main() -> None:
    host = os.environ.get("AGENT_NETWORK_HOST", "127.0.0.1")
    port = int(os.environ.get("AGENT_NETWORK_PORT", "8000"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Agent network running at http://{host}:{port}")
    print("Demo-only agent tokens are listed in README.md; bind to localhost only.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping agent network.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
