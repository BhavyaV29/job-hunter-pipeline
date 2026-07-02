"""Tests for job URL parsing and field normalization."""
from __future__ import annotations

import pytest

from job_quality import (
    normalize_job_fields,
    parse_cutshort_url_slug,
    SOURCE_NAMES,
)


@pytest.mark.parametrize(
    "url, expected_company, expected_role",
    [
        (
            "https://cutshort.io/job/Backend-Engineer-Python-Bengaluru-Bangalore-Trendlyne-Technologies-sDPS8NeY",
            "Trendlyne Technologies",
            "Backend Engineer Python",
        ),
        (
            "https://cutshort.io/job/Back-end-Engineer-Python-Golang-Bengaluru-Bangalore-HyrHub-QfDzEMWi",
            "Hyrhub",
            "Back End Engineer Python Golang",
        ),
        (
            "https://cutshort.io/job/Associate-ML-Engineer-GenAI-Bengaluru-Bangalore-Mumbai-Quantiphi-GeOW9o1L",
            "Quantiphi",
            "Associate Ml Engineer Genai",
        ),
        (
            "https://cutshort.io/job/Python-Developer-Pune-Wissen-Technology-Bq8PmNSS",
            "Wissen Technology",
            "Python Developer",
        ),
        (
            "https://cutshort.io/job/Associate-Software-Developer-Unnati-rKLlaxw1",
            "Unnati",
            "Associate Software Developer",
        ),
        (
            "https://cutshort.io/job/Data-Scientist-Machine-Learning-AI-GenAI-Focus-Bengaluru-Bangalore-Delhi-Gurugram-Noida-Ghaziabad-Faridabad-Chennai-Ampera-Technologies-fnof0jyW",
            "Ampera Technologies",
            "Data Scientist Machine Learning Ai Genai Focus",
        ),
        (
            "https://cutshort.io/job/Backend-Engineer-Node-js-Bengaluru-Bangalore-Improving-w6Zl8jmi",
            "Improving",
            "Backend Engineer Node Js",
        ),
        (
            "https://cutshort.io/job/Junior-Backend-Developer-Bengaluru-Bangalore-Enan-Tech-Private-Limited-eZYwjbcF",
            "Enan Tech Private Limited",
            "Junior Backend Developer",
        ),
    ],
)
def test_parse_cutshort_url_slug(url: str, expected_company: str, expected_role: str):
    company, role = parse_cutshort_url_slug(url)
    assert company == expected_company
    assert role == expected_role


def test_normalize_job_fields_cutshort_from_url():
    url = (
        "https://cutshort.io/job/Backend-Engineer-Python-Bengaluru-Bangalore"
        "-Trendlyne-Technologies-sDPS8NeY"
    )
    co, role = normalize_job_fields("cutshort", "Backend Engineer (Python)", url, "cutshort")
    assert co == "Trendlyne Technologies"
    assert co.lower() not in SOURCE_NAMES
    assert "Backend" in role


def test_normalize_job_fields_empty_company():
    url = "https://cutshort.io/job/Associate-Software-Developer-Unnati-rKLlaxw1"
    co, role = normalize_job_fields("", "Associate Software Developer", url, "cutshort")
    assert co == "Unnati"


def test_normalize_prefers_title_before_url_slug():
    url = "https://cutshort.io/job/Backend-Engineer-Python-Bengaluru-Bangalore-Trendlyne-Technologies-sDPS8NeY"
    co, role = normalize_job_fields("", "Backend Engineer at Trendlyne Technologies", url, "cutshort")
    assert co == "Trendlyne Technologies"
