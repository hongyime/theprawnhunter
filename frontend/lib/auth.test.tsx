import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, render, renderHook, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';

// Mock supabase
const mockGetSession = vi.hoisted(() => vi.fn());
const mockOnAuthStateChange = vi.hoisted(() => vi.fn());
const mockSignInWithPassword = vi.hoisted(() => vi.fn());
const mockSignOut = vi.hoisted(() => vi.fn());

vi.mock('./supabase', () => ({
  supabase: {
    auth: {
      getSession: mockGetSession,
      onAuthStateChange: mockOnAuthStateChange,
      signInWithPassword: mockSignInWithPassword,
      signOut: mockSignOut,
    },
  },
}));

import { AuthProvider, useAuth } from './auth';

// Test component that uses auth
function TestComponent() {
  const { session, user, loading, error, signIn, signOut } = useAuth();
  
  return (
    <div>
      <span data-testid="loading">{loading ? 'true' : 'false'}</span>
      <span data-testid="session">{session ? 'authenticated' : 'null'}</span>
      <span data-testid="user">{user?.email ?? 'null'}</span>
      <span data-testid="error">{error || 'none'}</span>
      <button onClick={() => signIn('test@example.com', 'password')}>Sign In</button>
      <button onClick={signOut}>Sign Out</button>
    </div>
  );
}

describe('AuthProvider', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockOnAuthStateChange.mockReturnValue({
      data: { subscription: { unsubscribe: vi.fn() } },
    });
  });

  it('throws when useAuth is called outside provider', () => {
    const consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    try {
      expect(() => renderHook(() => useAuth())).toThrow(
        'useAuth must be used within an AuthProvider'
      );
    } finally {
      consoleErrorSpy.mockRestore();
    }
  });
  it('shows loading state initially', async () => {
    mockGetSession.mockImplementation(() => new Promise(() => {})); // Never resolves
    
    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );

    expect(screen.getByTestId('loading').textContent).toBe('true');
  });

  it('sets session on successful getSession', async () => {
    const mockSession = { user: { email: 'test@example.com' } };
    mockGetSession.mockResolvedValue({ data: { session: mockSession }, error: null });

    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('loading').textContent).toBe('false');
      expect(screen.getByTestId('session').textContent).toBe('authenticated');
    });
  });

  it('sets error on getSession failure', async () => {
    mockGetSession.mockResolvedValue({ 
      data: { session: null }, 
      error: { message: 'Session error' } 
    });

    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('loading').textContent).toBe('false');
      expect(screen.getByTestId('error').textContent).toBe('Session error');
    });
  });

  it('handles getSession rejection', async () => {
    mockGetSession.mockRejectedValue(new Error('Network error'));

    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('loading').textContent).toBe('false');
      expect(screen.getByTestId('error').textContent).toBe('Network error');
    });
  });

  it('subscribes to onAuthStateChange and unsubscribes on unmount', async () => {
    const unsubscribe = vi.fn();
    mockGetSession.mockResolvedValue({ data: { session: null }, error: null });
    mockOnAuthStateChange.mockReturnValue({
      data: { subscription: { unsubscribe } },
    });

    const { unmount } = render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(mockOnAuthStateChange).toHaveBeenCalled();
    });

    unmount();
    expect(unsubscribe).toHaveBeenCalled();
  });

  it('updates session on auth state change', async () => {
    let authCallback: ((event: string, session: unknown) => void) = () => {};
    mockGetSession.mockResolvedValue({ data: { session: null }, error: null });
    mockOnAuthStateChange.mockImplementation((callback) => {
      authCallback = callback;
      return { data: { subscription: { unsubscribe: vi.fn() } } };
    });

    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(mockOnAuthStateChange).toHaveBeenCalled();
    });

    const mockSession = { user: { email: 'test@example.com', id: '123' }, access_token: 'token' };
    act(() => {
      authCallback('SIGNED_IN', mockSession);
    });

    await waitFor(() => {
      expect(screen.getByTestId('session').textContent).toBe('authenticated');
      expect(screen.getByTestId('user').textContent).toBe('test@example.com');
      expect(screen.getByTestId('loading').textContent).toBe('false');
    });
  });

  it('signIn returns error on failure', async () => {
    mockGetSession.mockResolvedValue({ data: { session: null }, error: null });
    mockSignInWithPassword.mockResolvedValue({ error: { message: 'Invalid credentials' } });

    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('loading').textContent).toBe('false');
    });

    const signInButton = screen.getByText('Sign In');
    await userEvent.click(signInButton);

    await waitFor(() => {
      expect(mockSignInWithPassword).toHaveBeenCalledWith({
        email: 'test@example.com',
        password: 'password',
      });
      expect(screen.getByTestId('error').textContent).toBe('Invalid credentials');
    });
  });

  it('sets session and resets loading on successful signIn', async () => {
    const mockUser = { email: 'test@example.com', id: '123' };
    const mockSession = { user: mockUser, access_token: 'token' };
    mockGetSession.mockResolvedValue({ data: { session: null }, error: null });
    mockSignInWithPassword.mockResolvedValue({
      data: { session: mockSession, user: mockUser },
      error: null,
    });

    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('loading').textContent).toBe('false');
    });

    const signInButton = screen.getByText('Sign In');
    await userEvent.click(signInButton);

    await waitFor(() => {
      expect(mockSignInWithPassword).toHaveBeenCalledWith({
        email: 'test@example.com',
        password: 'password',
      });
      expect(screen.getByTestId('session').textContent).toBe('authenticated');
      expect(screen.getByTestId('user').textContent).toBe('test@example.com');
      expect(screen.getByTestId('loading').textContent).toBe('false');
      expect(screen.getByTestId('error').textContent).toBe('none');
    });
  });

  it('returns error and remains unauthenticated when signIn succeeds without a session', async () => {
    mockGetSession.mockResolvedValue({ data: { session: null }, error: null });
    mockSignInWithPassword.mockResolvedValue({
      data: { session: null, user: null },
      error: null,
    });

    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('loading').textContent).toBe('false');
    });

    const signInButton = screen.getByText('Sign In');
    await userEvent.click(signInButton);

    await waitFor(() => {
      expect(screen.getByTestId('error').textContent).toBe(
        'Authentication failed: no session returned'
      );
      expect(screen.getByTestId('session').textContent).toBe('null');
      expect(screen.getByTestId('user').textContent).toBe('null');
      expect(screen.getByTestId('loading').textContent).toBe('false');
    });
  });

  it('signOut clears session and resets loading', async () => {
    const mockSession = { user: { email: 'test@example.com' } };
    mockGetSession.mockResolvedValue({ data: { session: mockSession }, error: null });
    mockSignOut.mockResolvedValue({ error: null });

    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('session').textContent).toBe('authenticated');
    });

    const signOutButton = screen.getByText('Sign Out');
    await userEvent.click(signOutButton);

    await waitFor(() => {
      expect(mockSignOut).toHaveBeenCalled();
      expect(screen.getByTestId('session').textContent).toBe('null');
      expect(screen.getByTestId('loading').textContent).toBe('false');
    });
  });
});
