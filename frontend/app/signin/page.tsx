"use client";

import { useState } from "react";
import { useAuth } from "@/lib/auth";
import { useRouter } from "next/navigation";
import { supabase } from "@/lib/supabase";
import NeoCard from "@/components/NeoCard";
import NeoButton from "@/components/NeoButton";

/**
 * Map a Supabase auth failure to a short, human sentence. The page never
 * renders the raw provider message.
 */
export function magicLinkErrorMessage(message: string): string {
    const normalized = message.toLowerCase();
    if (normalized.includes("rate") || normalized.includes("too many")) {
        return "Too many sign-in attempts. Wait a minute and try again.";
    }
    if (
        normalized.includes("signups not allowed") ||
        (normalized.includes("signup") && normalized.includes("disabled"))
    ) {
        return "Access is limited to operator accounts.";
    }
    return "We couldn't send a sign-in link. Check the address and try again.";
}


export default function SignInPage() {
    const { signIn, loading, error } = useAuth();
    const router = useRouter();
    const [email, setEmail] = useState("");
    const [password, setPassword] = useState("");
    const [localError, setLocalError] = useState<string | null>(null);
    const [oauthLoading, setOauthLoading] = useState(false);
    const [magicLinkLoading, setMagicLinkLoading] = useState(false);
    const [magicLinkSentTo, setMagicLinkSentTo] = useState<string | null>(null);

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        setLocalError(null);
        
        const { error: signInError } = await signIn(email, password);

        if (signInError) {
            setLocalError(signInError.message);
        } else {
            router.push("/");
        }
    };

    const handleGitHub = async () => {
        setOauthLoading(true);
        setLocalError(null);
        const { error: oauthError } = await supabase.auth.signInWithOAuth({
            provider: "github",
            options: {
                redirectTo: `${window.location.origin}/`,
            },
        });
        if (oauthError) {
            setLocalError(oauthError.message);
            setOauthLoading(false);
        }
        // On success Supabase redirects the browser — no need to setLoading(false)
    };

    const handleMagicLink = async (event: React.FormEvent) => {
        event.preventDefault();
        setLocalError(null);
        const address = email.trim();
        if (!address) {
            setLocalError("Enter your email address to receive a sign-in link.");
            return;
        }
        setMagicLinkLoading(true);
        try {
            const { error: otpError } = await supabase.auth.signInWithOtp({
                email: address,
                options: {
                    emailRedirectTo: `${window.location.origin}/`,
                },
            });
            if (otpError) {
                setLocalError(magicLinkErrorMessage(otpError.message));
            } else {
                setMagicLinkSentTo(address);
            }
        } catch {
            setLocalError("We couldn't send a sign-in link. Please try again.");
        } finally {
            setMagicLinkLoading(false);
        }
    };

    return (
        <div className="min-h-screen flex items-center justify-center bg-slate-100">
            <NeoCard className="max-w-md w-full space-y-8 p-8">
                <div>
                    <h2 className="text-center text-3xl font-bold text-slate-900">
                        The Prawn Hunter
                    </h2>
                    <p className="mt-2 text-center text-sm text-slate-600">
                        Sign in to access the evidence dashboard
                    </p>
                    <p className="mt-1 text-center text-xs text-slate-500">
                        Access is limited to operator accounts.
                    </p>
                </div>

                {/* GitHub SSO — primary login method */}
                <div>
                    <NeoButton
                        type="button"
                        onClick={handleGitHub}
                        disabled={oauthLoading || loading}
                        className="w-full flex items-center justify-center gap-3 px-4 py-3 text-sm text-slate-700 disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                        {/* GitHub icon */}
                        <svg className="h-5 w-5" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                            <path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0 0 24 12c0-6.63-5.37-12-12-12z" />
                        </svg>
                        {oauthLoading ? "Redirecting…" : "Sign in with GitHub"}
                    </NeoButton>
                </div>

                <div className="relative">
                    <div className="absolute inset-0 flex items-center">
                        <div className="w-full border-t border-slate-200" />
                    </div>
                    <div className="relative flex justify-center text-xs uppercase">
                        <span className="bg-white px-2 text-slate-400">or email</span>
                    </div>
                </div>

                {magicLinkSentTo ? (
                    <div
                        role="status"
                        aria-live="polite"
                        className="space-y-3 rounded-md border border-emerald-200 bg-emerald-50 p-4 text-center"
                    >
                        <p className="text-sm font-medium text-emerald-900">
                            Check your inbox — link sent to {magicLinkSentTo}
                        </p>
                        <button
                            type="button"
                            onClick={() => setMagicLinkSentTo(null)}
                            className="text-sm font-medium text-emerald-800 underline hover:text-emerald-900"
                        >
                            Use a different email
                        </button>
                    </div>
                ) : (
                    <form className="space-y-6" onSubmit={handleSubmit}>
                        <div className="space-y-4">
                            <div>
                                <label htmlFor="email" className="block text-sm font-medium text-slate-700">
                                    Email address
                                </label>
                                <input
                                    id="email"
                                    name="email"
                                    type="email"
                                    autoComplete="email"
                                    required
                                    value={email}
                                    onChange={(e) => setEmail(e.target.value)}
                                    className="mt-1 appearance-none relative block w-full px-3 py-2 border border-slate-300 rounded-md placeholder-slate-400 focus:outline-none focus:ring-cyan-500 focus:border-cyan-500 sm:text-sm"
                                    placeholder="you@example.com"
                                />
                            </div>

                            <div>
                                <label htmlFor="password" className="block text-sm font-medium text-slate-700">
                                    Password
                                </label>
                                <input
                                    id="password"
                                    name="password"
                                    type="password"
                                    autoComplete="current-password"
                                    required
                                    value={password}
                                    onChange={(e) => setPassword(e.target.value)}
                                    className="mt-1 appearance-none relative block w-full px-3 py-2 border border-slate-300 rounded-md placeholder-slate-400 focus:outline-none focus:ring-cyan-500 focus:border-cyan-500 sm:text-sm"
                                    placeholder="••••••••"
                                />
                            </div>
                        </div>

                        {(error || localError) && (
                            <div className="rounded-md bg-red-50 p-4">
                                <p role="alert" className="text-sm text-red-800">{error || localError}</p>
                            </div>
                        )}

                        <div className="space-y-3">
                            <NeoButton
                                type="submit"
                                disabled={loading || magicLinkLoading}
                                className="w-full flex justify-center py-2 px-4 text-sm font-medium text-slate-900 disabled:opacity-50 disabled:cursor-not-allowed"
                            >
                                {loading ? "Signing in..." : "Sign in"}
                            </NeoButton>
                            <NeoButton
                                type="button"
                                onClick={handleMagicLink}
                                disabled={magicLinkLoading || loading || oauthLoading}
                                className="w-full flex justify-center py-2 px-4 text-sm font-medium text-slate-900 disabled:opacity-50 disabled:cursor-not-allowed"
                            >
                                {magicLinkLoading ? "Sending link..." : "Email me a sign-in link"}
                            </NeoButton>
                        </div>
                    </form>
                )}

                <div className="border-t border-slate-200 pt-6 text-center">
                    <a
                        href="/about"
                        className="text-sm font-medium text-cyan-700 hover:text-cyan-800 hover:underline"
                    >
                        Learn more about The Prawn Hunter
                    </a>
                </div>
            </NeoCard>
        </div>
    );
}
