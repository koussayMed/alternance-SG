import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fakes import VISITOR, FakeRepository, FakeWeatherClient

from app import create_app
from app.config import Settings


@pytest.fixture
def settings():
    return Settings(app_version="test-sha", environment="test", max_cities=3)


@pytest.fixture
def repo():
    return FakeRepository()


@pytest.fixture
def weather_client():
    return FakeWeatherClient()


@pytest.fixture
def app(settings, repo, weather_client):
    app = create_app(settings, repository=repo, weather_client=weather_client)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    """Test client sending the visitor id, like the frontend."""
    test_client = app.test_client()
    test_client.environ_base["HTTP_X_VISITOR_ID"] = VISITOR
    return test_client


@pytest.fixture
def lille(client):
    """Adds Lille and returns its JSON."""
    return client.post("/api/cities", json={"name": "Lille"}).get_json()
