import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SignInPage from "./page";

// Mock next/navigation
const mockPush = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: mockPush,
  }),
}));

// Mock useAuth hook
const mockSignIn = vi.fn();
const mockUseAuth = vi.fn();

vi.mock("@/lib/auth", () => ({
  useAuth: () => mockUseAuth(),
}));

const mockSignInWithOtp = vi.hoisted(() => vi.fn());
const mockSignInWithOAuth = vi.hoisted(() => vi.fn());

vi.mock("@/lib/supabase", () => ({
  supabase: {
    auth: {
      signInWithOtp: mockSignInWithOtp,
      signInWithOAuth: mockSignInWithOAuth,
    },
  },
}));

describe("SignInPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUseAuth.mockReturnValue({
      signIn: mockSignIn,
      loading: false,
      error: null,
    });
    mockSignInWithOtp.mockResolvedValue({ data: {}, error: null });
  });

  it("renders sign in form", () => {
    render(<SignInPage />);
    
    expect(screen.getByText("The Prawn Hunter")).toBeInTheDocument();
    expect(screen.getByLabelText(/email address/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/password/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^sign in$/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^sign in with github$/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /email me a sign-in link/i })).toBeInTheDocument();
    expect(screen.getByText(/access is limited to operator accounts/i)).toBeInTheDocument();
    expect(screen.getByText(/learn more about the prawn hunter/i).closest("a"))
      .toHaveAttribute("href", "/about");
  });

  it("sends a magic link to the typed address with the current origin", async () => {
    render(<SignInPage />);

    const emailInput = screen.getByLabelText(/email address/i);
    await userEvent.type(emailInput, "operator@example.com");
    await userEvent.click(screen.getByRole("button", { name: /email me a sign-in link/i }));

    await waitFor(() => {
      expect(mockSignInWithOtp).toHaveBeenCalledWith({
        email: "operator@example.com",
        options: { emailRedirectTo: `${window.location.origin}/` },
      });
    });

    expect(
      await screen.findByText(/check your inbox — link sent to operator@example\.com/i),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText(/email address/i)).not.toBeInTheDocument();
  });

  it("shows a human message when the magic link cannot be sent", async () => {
    mockSignInWithOtp.mockResolvedValueOnce({
      data: {},
      error: { message: "Email rate limit exceeded" },
    });
    render(<SignInPage />);

    await userEvent.type(screen.getByLabelText(/email address/i), "operator@example.com");
    await userEvent.click(screen.getByRole("button", { name: /email me a sign-in link/i }));

    expect(
      await screen.findByText("Too many sign-in attempts. Wait a minute and try again."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Email rate limit exceeded")).not.toBeInTheDocument();
    expect(screen.getByText(/access is limited to operator accounts/i)).toBeInTheDocument();
  });

  it("disables the magic link button while the request is in flight", async () => {
    let release!: (value: { data: object; error: null }) => void;
    mockSignInWithOtp.mockImplementationOnce(
      () => new Promise((resolve) => {
        release = resolve;
      }),
    );
    render(<SignInPage />);

    await userEvent.type(screen.getByLabelText(/email address/i), "operator@example.com");
    await userEvent.click(screen.getByRole("button", { name: /email me a sign-in link/i }));

    const pendingButton = screen.getByRole("button", { name: /sending link/i });
    expect(pendingButton).toBeDisabled();

    release({ data: {}, error: null });

    expect(
      await screen.findByText(/check your inbox — link sent to operator@example\.com/i),
    ).toBeInTheDocument();
  });

  it("shows error when signIn returns error - does NOT navigate", async () => {
    const errorMessage = "Invalid login credentials";
    mockSignIn.mockResolvedValueOnce({ error: { message: errorMessage } });

    render(<SignInPage />);
    
    const emailInput = screen.getByLabelText(/email address/i);
    const passwordInput = screen.getByLabelText(/password/i);
    const submitButton = screen.getByRole("button", { name: /^sign in$/i });

    await userEvent.type(emailInput, "test@example.com");
    await userEvent.type(passwordInput, "wrongpassword");
    fireEvent.click(submitButton);

    await waitFor(() => {
      expect(mockSignIn).toHaveBeenCalledWith("test@example.com", "wrongpassword");
    }, { timeout: 10000 });

    await waitFor(() => {
      expect(screen.getByText(errorMessage)).toBeInTheDocument();
    }, { timeout: 10000 });

    // CRITICAL: Should NOT navigate on error
    expect(mockPush).not.toHaveBeenCalled();
  }, 15000);

  it("navigates to home on successful signIn", async () => {
    mockSignIn.mockResolvedValueOnce({ error: null });

    render(<SignInPage />);
    
    const emailInput = screen.getByLabelText(/email address/i);
    const passwordInput = screen.getByLabelText(/password/i);
    const submitButton = screen.getByRole("button", { name: /^sign in$/i });

    await userEvent.type(emailInput, "test@example.com");
    await userEvent.type(passwordInput, "correctpassword");
    fireEvent.click(submitButton);

    await waitFor(() => {
      expect(mockSignIn).toHaveBeenCalledWith("test@example.com", "correctpassword");
    }, { timeout: 10000 });

    await waitFor(() => {
      expect(mockPush).toHaveBeenCalledWith("/");
    }, { timeout: 10000 });
  }, 15000);

  it("disables submit button while loading", () => {
    mockUseAuth.mockReturnValue({
      signIn: mockSignIn,
      loading: true,
      error: null,
    });

    render(<SignInPage />);
    
    const submitButton = screen.getByRole("button", { name: /signing in\.\.\./i });
    expect(submitButton).toBeDisabled();
  });

  it("displays auth context error", () => {
    mockUseAuth.mockReturnValue({
      signIn: mockSignIn,
      loading: false,
      error: "Session expired",
    });

    render(<SignInPage />);
    
    expect(screen.getByText("Session expired")).toBeInTheDocument();
  });
});
