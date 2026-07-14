from staffing_filter import is_staffing_listing
from job_quality import is_invalid_company, is_spam_url


def test_synergistic_company():
    assert is_staffing_listing("SynergisticIT", "https://jobgether.com/x", "Backend")


def test_talentzo_recruiter_title():
    assert is_staffing_listing(
        "Talentzo Delhi", "https://naukri.com/j",
        "Software Engineer at A FinTech Company",
    )


def test_fortified_staffing():
    assert is_staffing_listing("Fortified Infotech", "https://example.com/j", "Backend")


def test_repost_portals_are_staffing():
    assert is_staffing_listing("iBrowseJobs", "https://example.com/j", "Backend Engineer")
    assert is_staffing_listing("WonksKnow", "https://example.com/j", "Python Developer")


def test_placement_service_company_is_staffing():
    assert is_staffing_listing(
        "Acme Placement Services",
        "https://example.com/j",
        "Software Engineer",
    )


def test_trainer_and_staffing_repost_titles_are_staffing():
    assert is_staffing_listing(
        "Career Academy",
        "https://example.com/j",
        "Python Trainer - Night Shift",
    )
    assert is_staffing_listing(
        "Acme",
        "https://example.com/j",
        "Backend Engineer reposted via staffing partner",
    )


def test_railway_spam_url():
    assert is_spam_url("https://frontendnode-production.up.railway.app/jobs/1")


def test_railway_company_invalid():
    assert is_invalid_company("frontendnode-production.up.railway.app", "")


def test_tbd_invalid():
    assert is_invalid_company("TBD", "https://wellfound.com/jobs/1")


def test_real_company_not_staffing():
    assert not is_staffing_listing("Stripe", "https://stripe.com/jobs", "Backend Engineer")
    assert not is_staffing_listing(
        "TrainerRoad",
        "https://trainerroad.com/careers/backend",
        "Backend Engineer",
    )


def test_phonepe_not_invalid():
    assert not is_invalid_company("PhonePe", "https://job-boards.greenhouse.io/phonepe/j")
