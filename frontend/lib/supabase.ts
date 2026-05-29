"use client";

import { createClient } from "@supabase/supabase-js";
import { config } from "./config";

export function browserSupabase() {
  if (!config.supabaseUrl || !config.supabaseKey) {
    throw new Error("Supabase frontend environment variables are not configured.");
  }
  return createClient(config.supabaseUrl, config.supabaseKey);
}
