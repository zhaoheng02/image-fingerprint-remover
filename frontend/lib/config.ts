export const config = {
  supabaseUrl: process.env.NEXT_PUBLIC_SUPABASE_URL ?? "",
  supabaseKey: process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY ?? "",
  apiBaseUrl: process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000",
  requireAuth: process.env.NEXT_PUBLIC_REQUIRE_AUTH !== "false",
  billingEnabled: process.env.NEXT_PUBLIC_BILLING_ENABLED === "true"
};
