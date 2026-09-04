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

describe("SignInPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockUseAuth.mockReturnValue({
      signIn: mockSignIn,
      loading: false,
      error: null,
    });
  });

  it("renders sign in form", () => {
    render(<SignInPage />);
    
    expect(screen.getByText("Telegram Hunter")).toBeInTheDocument();
    expect(screen.getByLabelText(/email address/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/password/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /sign in/i })).toBeInTheDocument();
  });

  it("shows error when signIn returns error - does NOT navigate", async () => {
    const errorMessage = "Invalid login credentials";
    mockSignIn.mockResolvedValueOnce({ error: { message: errorMessage } });

    render(<SignInPage />);
    
    const emailInput = screen.getByLabelText(/email address/i);
    const passwordInput = screen.getByLabelText(/password/i);
    const submitButton = screen.getByRole("button", { name: /sign in/i });

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
    const submitButton = screen.getByRole("button", { name: /sign in/i });

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
