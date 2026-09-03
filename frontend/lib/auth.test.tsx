import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { AuthProvider, useAuth } from '../lib/auth';

// Mock supabase
const mockGetSession = vi.fn();
const mockOnAuthStateChange = vi.fn();
const mockSignInWithPassword = vi.fn();
const mockSignOut = vi.fn();

vi.mock('../lib/supabase', () => ({
  supabase: {
    auth: {
      getSession: mockGetSession,
      onAuthStateChange: mockOnAuthStateChange,
      signInWithPassword: mockSignInWithPassword,
      signOut: mockSignOut,
    },
  },
}));

// Test component that uses auth
function TestComponent() {
  const { session, loading, error, signIn, signOut } = useAuth();
  
  return (
    <div>
      <span data-testid="loading">{loading ? 'true' : 'false'}</span>
      <span data-testid="session">{session ? 'authenticated' : 'null'}</span>
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

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('detects missing provider with undefined default', () => {
    // Context defaults to undefined, not a fake object
    const { AuthContext } = require('@/lib/auth');
    expect(AuthContext._currentValue).toBeUndefined();
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
    let authCallback: any;
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

    // Simulate successful sign in
    const mockSession = { user: { email: 'test@example.com' } };
    authCallback('SIGNED_IN', mockSession);

    await waitFor(() => {
      expect(screen.getByTestId('session').textContent).toBe('authenticated');
      expect(screen.getByTestId('loading').textContent).toBe('false');
    });
  });

  it('signIn returns error on failure', async () => {
    const mockError = new Error('Invalid credentials');
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

  it('resets loading on successful signIn', async () => {
    mockGetSession.mockResolvedValue({ data: { session: null }, error: null });
    let authCallback: any;
    mockOnAuthStateChange.mockImplementation((callback) => {
      authCallback = callback;
      return { data: { subscription: { unsubscribe: vi.fn() } } };
    });
    mockSignInWithPassword.mockResolvedValue({ error: null });

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

    // Loading should reset via onAuthStateChange
    authCallback('SIGNED_IN', { user: { email: 'test@example.com' } });

    await waitFor(() => {
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
