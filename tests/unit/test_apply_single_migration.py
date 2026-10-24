from scripts.deploy.apply_single_migration import project_ref_from_url


def test_project_ref_from_url_with_path_and_scheme():
    assert project_ref_from_url("https://abc123xyz.supabase.co") == "abc123xyz"
    assert project_ref_from_url("https://abc123xyz.supabase.co/") == "abc123xyz"


def test_project_ref_from_bare_host():
    assert project_ref_from_url("abc123xyz.supabase.co") == "abc123xyz"
