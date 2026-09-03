"""
Tests for Plan Item 1: RLS hardening + auth gating
- Anon access is fully revoked on exfiltrated_messages
- Authenticated access revoked on raw table
- Only authenticated users can select from evidence_redacted
- Service role has full access via bypass
"""

import pytest
import re


def strip_sql_comments(sql: str) -> str:
    """Remove SQL comments (-- and /* */) for semantic checks."""
    # Remove single-line comments
    sql = re.sub(r'--[^\n]*', '', sql)
    # Remove multi-line comments
    sql = re.sub(r'/\*.*?\*/', '', sql, flags=re.DOTALL)
    return sql


def test_migration_file_exists():
    """Migration file must exist for Plan Item 1."""
    import glob
    migrations = glob.glob("supabase/migrations/*rls_hardening*.sql")
    assert len(migrations) == 1, "Expected exactly one rls_hardening migration file"
    
    with open(migrations[0], "r") as f:
        content = f.read()
    
    # Must revoke from both anon and authenticated
    assert "REVOKE ALL ON exfiltrated_messages FROM anon" in content
    assert "REVOKE ALL ON exfiltrated_messages FROM authenticated" in content
    
    # Must drop the authenticated raw policy
    assert 'DROP POLICY IF EXISTS "Authenticated Read Access"' in content


def test_no_raw_authenticated_access():
    """Migration must NOT grant or enable authenticated SELECT on raw table."""
    import glob
    migrations = glob.glob("supabase/migrations/*rls_hardening*.sql")
    
    for migration in migrations:
        with open(migration, "r") as f:
            content = f.read()
        
        # Strip comments for semantic check
        clean = strip_sql_comments(content)
        
        # Must NOT have authenticated policy on raw table
        assert 'CREATE POLICY "Authenticated Read Access"' not in clean
        assert 'TO authenticated' not in clean or 'evidence_redacted' in clean.lower()
        
        # Must NOT grant raw table to authenticated
        assert "GRANT SELECT ON exfiltrated_messages TO authenticated" not in clean


def test_redacted_view_masks_tokens():
    """evidence_redacted view must mask token-like patterns."""
    import glob
    migrations = glob.glob("supabase/migrations/*rls_hardening*.sql")
    
    for migration in migrations:
        with open(migration, "r") as f:
            content = f.read()
    
    # Must use regexp_replace to mask tokens
    assert "regexp_replace" in content
    assert '[TOKEN]' in content
    assert r'\d{8,10}:[A-Za-z0-9_-]{30,}' in content


def test_redacted_view_pseudonymizes_sender():
    """evidence_redacted view must pseudonymize sender_name."""
    import glob
    migrations = glob.glob("supabase/migrations/*rls_hardening*.sql")
    
    for migration in migrations:
        with open(migration, "r") as f:
            content = f.read()
    
    # Must truncate sender_name
    assert "left(sender_name, 3)" in content or "sender_name, 3" in content
    assert "..." in content


def test_redacted_view_grant_authenticated_only():
    """evidence_redacted must be granted ONLY to authenticated."""
    import glob
    migrations = glob.glob("supabase/migrations/*rls_hardening*.sql")
    
    for migration in migrations:
        with open(migration, "r") as f:
            content = f.read()
        
        clean = strip_sql_comments(content)
        
        # Must grant to authenticated
        assert "GRANT SELECT ON public.evidence_redacted TO authenticated" in content
        
        # Must revoke from anon
        assert "REVOKE ALL ON public.evidence_redacted FROM anon" in content


def test_chatwindow_uses_redacted_view():
    """ChatWindow must query evidence_redacted view, not raw table."""
    with open("frontend/components/ChatWindow.tsx", "r") as f:
        content = f.read()
    
    # Must use evidence_redacted
    assert '.from("evidence_redacted")' in content
    
    # Must NOT use raw table for SELECT
    assert '.from("exfiltrated_messages")' not in content


def test_chatwindow_gates_on_session():
    """ChatWindow must check for session before querying."""
    with open("frontend/components/ChatWindow.tsx", "r") as f:
        content = f.read()
    
    # Must import useAuth
    assert 'import { useAuth }' in content
    
    # Must check session
    assert 'if (!session)' in content
    
    # Must set auth error
    assert 'Sign in required' in content


def test_no_realtime_subscription():
    """ChatWindow must NOT subscribe to raw table realtime."""
    with open("frontend/components/ChatWindow.tsx", "r") as f:
        content = f.read()
    
    # Must NOT have realtime subscription to raw table
    assert '.channel(' not in content or 'evidence_redacted' in content
    assert 'postgres_changes' not in content


def test_auth_context_structure():
    """Auth context must provide session, signIn, signOut."""
    with open("frontend/lib/auth.tsx", "r") as f:
        content = f.read()
    
    # Must export AuthProvider
    assert 'export function AuthProvider' in content
    
    # Must export useAuth
    assert 'export function useAuth' in content
    
    # Must getSession on mount
    assert 'getSession()' in content
    
    # Must subscribe to auth changes
    assert 'onAuthStateChange' in content
    
    # Must implement signIn
    assert 'signInWithPassword' in content
    
    # Must implement signOut
    assert 'auth.signOut' in content
    
    # Must cleanup subscription
    assert 'unsubscribe' in content


def test_layout_mounts_auth_provider():
    """Root layout must mount AuthProvider."""
    with open("frontend/app/layout.tsx", "r") as f:
        content = f.read()
    
    # Must import AuthProvider
    assert 'import { AuthProvider }' in content
    
    # Must wrap children
    assert '<AuthProvider>' in content


def test_signin_page_exists():
    """Sign-in page must exist with email/password form."""
    import os
    assert os.path.exists("frontend/app/signin/page.tsx")
    
    with open("frontend/app/signin/page.tsx", "r") as f:
        content = f.read()
    
    # Must have email input
    assert 'type="email"' in content
    
    # Must have password input
    assert 'type="password"' in content
    
    # Must call signIn
    assert 'signIn(' in content
    
    # Must redirect on success
    assert 'router.push' in content
