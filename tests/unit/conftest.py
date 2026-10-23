"""
Shared fixtures for Daily Data Pipeline tests.
"""
import json
import pytest
from pathlib import Path
from datetime import datetime


@pytest.fixture
def sample_daily_record():
    """Sample daily electricity record for testing (room_name as primary key)."""
    return {
        "校区": "仙林校区",
        "楼栋": "19幢",
        "房间": "19栋第16层1613",
        "剩余电量": "125.50度",
        "timestamp": "2026-05-15T02:00:00Z",
        "success": True
    }


@pytest.fixture
def sample_room_ids():
    """Sample room IDs for testing."""
    return ["53463", "53464", "53465"]


@pytest.fixture
def temp_database(tmp_path):
    """Create temporary database directory structure for testing."""
    database = tmp_path / "database"
    database.mkdir()
    
    # Create sample campus structure (room_name as directory name, no room_id suffix)
    campus_dir = database / "仙林校区" / "19幢" / "19栋第16层1613"
    campus_dir.mkdir(parents=True)
    
    # Create sample daily file
    sample_file = campus_dir / "20260515.json"
    sample_data = {
        "校区": "仙林校区",
        "楼栋": "19幢",
        "房间": "19栋第16层1613",
        "剩余电量": "125.50度",
        "timestamp": "2026-05-15T02:00:00Z",
        "success": True
    }
    
    with open(sample_file, 'w', encoding='utf-8') as f:
        json.dump(sample_data, f, ensure_ascii=False, indent=2)
    
    return database


@pytest.fixture
def temp_config(tmp_path, sample_room_ids):
    """Create temporary config directory with room IDs for testing."""
    config = tmp_path / "config"
    config.mkdir()
    
    room_ids_file = config / "room_ids.txt"
    with open(room_ids_file, 'w', encoding='utf-8') as f:
        for room_id in sample_room_ids:
            f.write(f"{room_id}\n")
    
    return config


@pytest.fixture
def sample_cookie_json():
    """Sample cookie JSON for testing."""
    return [
        {
            "name": "JSESSIONID",
            "value": "ABC123DEF456",
            "domain": "epay.nju.edu.cn",
            "path": "/",
            "expires": -1,
            "httpOnly": False,
            "secure": False
        }
    ]


@pytest.fixture
def temp_cookie_file(tmp_path, sample_cookie_json):
    """Create temporary cookie file for testing."""
    cookie_file = tmp_path / "cookie.json"
    with open(cookie_file, 'w', encoding='utf-8') as f:
        json.dump(sample_cookie_json, f, ensure_ascii=False, indent=2)
    
    return cookie_file


@pytest.fixture
def sample_summary_data():
    """Sample aggregated summary data for testing."""
    return {
        "generated_at": "2026-05-15T02:05:00Z",
        "total_rooms": 1,
        "query_success_rate": 1.0,
        "rooms": {
            "19栋第16层1613": {
                "campus": "仙林校区",
                "building": "19幢",
                "room": "19栋第16层1613",
                "current_balance": 125.50,
                "avg_7d": 128.30,
                "avg_30d": 130.45,
                "trend_30d": -0.15,
                "min_30d": 120.00,
                "max_30d": 135.20,
                "last_updated": "2026-05-15T02:00:00Z"
            }
        }
    }
