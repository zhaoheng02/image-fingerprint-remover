import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ImgClean",
  description: "Privacy-first image fingerprint cleanup for teams and creators."
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
