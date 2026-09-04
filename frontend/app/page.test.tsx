import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';

// Use vi.hoisted to create mock functions before module import
const mockGetSession = vi.hoisted(() => vi.fn());
const mockOnAuthStateChange = vi.hoisted(() => vi.fn());
const mockSignInWithPassword = vi.hoisted(() => vi.fn());
const mockSignOut = vi.hoisted(() => vi.fn());

// Mock the supabase module
vi.mock('../lib/supabase', () => ({
  supabase: {
    auth: {
      getSession: mockGetSession,
      onAuthStateChange: mockOnAuthStateChange,
      signInWithPassword: mockSignInWithPassword,
      signOut: mockSignOut,
    },
    from: vi.fn(() => {
      const builder = {
        select: vi.fn(),
        eq: vi.fn(),
        limit: vi.fn(),
      };
      builder.select.mockReturnValue(builder);
      builder.eq.mockReturnValue(builder);
      builder.limit.mockResolvedValue({
        data: [{ id: 'test-uuid', created_at: '2024-01-01', source: 'test' }],
        error: null,
      });
      return builder;
    }),
  },
}));

// Mock Next.js navigation
vi.mock('next/navigation', () => ({
  useRouter: () => ({
    push: vi.fn(),
    refresh: vi.fn(),
  }),
  usePathname: () => '/',
}));

// Import types for accurate mock contracts
import type { Credential, DashboardView } from './page';

// Mock child components - Sidebar captures production contract
vi.mock('@/components/Sidebar', () => ({
  default: ({ selected, activeView, onViewChange, onSelect }: {
    selected?: Credential | null;
    activeView?: DashboardView;
    onViewChange?: (view: DashboardView) => void;
    onSelect?: (cred: Credential) => void;
  }) => (
    <div data-testid="sidebar">
      <span data-testid="sidebar-selected">{selected?.id ?? 'none'}</span>
      <span data-testid="sidebar-activeView">{activeView}</span>
      <button data-testid="sidebar-view-change" onClick={() => onViewChange && onViewChange('botTelemetry')} disabled={!onViewChange}>
        Change View
      </button>
      <button data-testid="sidebar-select" onClick={() => onSelect && onSelect({ id: 'test-uuid', created_at: '2024-01-01', source: 'test' } as Credential)} disabled={!onSelect}>
        Select
      </button>
    </div>
  ),
}));

vi.mock('@/components/ChatWindow', () => ({
  default: () => (
    <div data-testid="chat-window">Chat Window</div>
  ),
}));

vi.mock('@/components/FindingsQueue', () => ({
  default: ({ onDrilldown }: {
    onDrilldown?: (credentialId: string, view: 'chat' | 'botTelemetry') => void;
  }) => (
    <div data-testid="findings-queue">
      Findings Queue
      <button data-testid="findings-drilldown" onClick={() => onDrilldown?.('test-uuid', 'chat')}>
        Open Chat
      </button>
    </div>
  ),
}));

vi.mock('@/components/TelemetryAnalyticsView', () => ({
  default: () => (
    <div data-testid="telemetry-view">Telemetry View</div>
  ),
}));

// Import after mocking
import { AuthProvider } from '../lib/auth';
import Home from './page';

describe('Dashboard Gate (Production page.tsx)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockOnAuthStateChange.mockReturnValue({
      data: { subscription: { unsubscribe: vi.fn() } },
    });
    // Mock window.resizeTo for mobile tests
    window.innerWidth = 1024;
  });

  it('shows loading state while checking session', async () => {
    mockGetSession.mockImplementation(() => new Promise(() => {}));

    render(
      <AuthProvider>
        <Home />
      </AuthProvider>
    );

    expect(screen.getByText('Authenticating...')).toBeDefined();
    // Sidebar and ChatWindow should NOT be mounted
    expect(screen.queryByTestId('sidebar')).toBeNull();
    expect(screen.queryByTestId('chat-window')).toBeNull();
  });

  it('shows sign-in prompt when unauthenticated', async () => {
    mockGetSession.mockResolvedValue({
      data: { session: null },
      error: null
    });

    render(
      <AuthProvider>
        <Home />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByText('Telegram Hunter')).toBeDefined();
      expect(screen.getByText('Sign in required to access the dashboard')).toBeDefined();
      expect(screen.getByText('Sign In').closest('a')).toHaveProperty('href', expect.stringContaining('/signin'));
    });

    // Sidebar and ChatWindow should NOT be mounted
    expect(screen.queryByTestId('sidebar')).toBeNull();
    expect(screen.queryByTestId('chat-window')).toBeNull();
  });

  it('shows authentication error in sign-in prompt', async () => {
    mockGetSession.mockResolvedValue({
      data: { session: null },
      error: { message: 'Session expired' }
    });

    render(
      <AuthProvider>
        <Home />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByText('Telegram Hunter')).toBeDefined();
      expect(screen.getByText('Session expired')).toBeDefined();
    });

    // Should still show sign-in link
    expect(screen.getByText('Sign In')).toBeDefined();
  });

  it('shows dashboard when authenticated', async () => {
    const mockSession = { user: { email: 'test@example.com' } };
    mockGetSession.mockResolvedValue({
      data: { session: mockSession },
      error: null
    });

    render(
      <AuthProvider>
        <Home />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('sidebar')).toBeDefined();
      expect(screen.getByTestId('findings-queue')).toBeDefined();
    });

    // Should show user email
    expect(screen.getByText('test@example.com')).toBeDefined();
    // Should NOT show sign-in prompt
    expect(screen.queryByText('Telegram Hunter')).toBeNull();
  });

  it('clicks select button and shows selected credential id', async () => {
    const mockSession = { user: { email: 'test@example.com' } };
    mockGetSession.mockResolvedValue({
      data: { session: mockSession },
      error: null
    });

    render(
      <AuthProvider>
        <Home />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('sidebar')).toBeDefined();
    });

    // Click select button in Sidebar mock
    const selectButton = screen.getByTestId('sidebar-select');
    await userEvent.click(selectButton);

    // Verify selected id is rendered
    await waitFor(() => {
      expect(screen.getByTestId('sidebar-selected').textContent).toBe('test-uuid');
    });
  });

  it('clicks view-change button and renders bot telemetry', async () => {
    const mockSession = { user: { email: 'test@example.com' } };
    mockGetSession.mockResolvedValue({
      data: { session: mockSession },
      error: null
    });

    render(
      <AuthProvider>
        <Home />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('sidebar')).toBeDefined();
    });

    // Click view-change button in Sidebar mock
    const viewChangeButton = screen.getByTestId('sidebar-view-change');
    await userEvent.click(viewChangeButton);

    // Verify telemetry view is rendered
    await waitFor(() => {
      expect(screen.getByTestId('telemetry-view')).toBeDefined();
    });
  });

  it('sign-out button calls signOut', async () => {
    const mockSession = { user: { email: 'test@example.com' } };
    mockGetSession.mockResolvedValue({
      data: { session: mockSession },
      error: null
    });
    mockSignOut.mockResolvedValue({ error: null });

    render(
      <AuthProvider>
        <Home />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('sidebar')).toBeDefined();
    });

    // Desktop: Sign Out button in header
    const signOutButton = screen.getByText('Sign Out').closest('button')!;
    await userEvent.click(signOutButton);

    await waitFor(() => {
      expect(mockSignOut).toHaveBeenCalled();
    });
  });

  it('sidebar and ChatWindow do NOT mount when unauthenticated', async () => {
    mockGetSession.mockResolvedValue({
      data: { session: null },
      error: null
    });

    render(
      <AuthProvider>
        <Home />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByText('Sign in required to access the dashboard')).toBeDefined();
    });

    // CRITICAL: Sidebar and ChatWindow should NOT be in the DOM
    expect(screen.queryByTestId('sidebar')).toBeNull();
    expect(screen.queryByTestId('chat-window')).toBeNull();
    expect(screen.queryByTestId('findings-queue')).toBeNull();
    expect(screen.queryByTestId('telemetry-view')).toBeNull();
  });

  it('shows all view tabs when authenticated', async () => {
    const mockSession = { user: { email: 'test@example.com' } };
    mockGetSession.mockResolvedValue({
      data: { session: mockSession },
      error: null
    });

    render(
      <AuthProvider>
        <Home />
      </AuthProvider>
    );

    await waitFor(() => {
      // Assert Sidebar received production contract props
      const sidebar = screen.getByTestId('sidebar');
      expect(sidebar).toBeDefined();

      // Assert activeView prop passed
      const activeViewEl = screen.getByTestId('sidebar-activeView');
      expect(activeViewEl.textContent).toBe('findings');

      // Assert callback props are wired (buttons enabled)
      const viewChangeBtn = screen.getByTestId('sidebar-view-change');
      const selectBtn = screen.getByTestId('sidebar-select');
      expect(viewChangeBtn).not.toBeDisabled();
      expect(selectBtn).not.toBeDisabled();
    });
  });
});
