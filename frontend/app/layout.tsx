import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";
import DemoStatusIndicator from "@/components/DemoStatusIndicator";
import DemoWelcomeBanner from "@/components/DemoWelcomeBanner";
import DemoPublicBanner from "@/components/DemoPublicBanner";

export const metadata: Metadata = {
  title: "SiteMedic",
  description: "Autonomous SRE agent powered by Gemini + Dynatrace",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body className="bg-gray-950 text-gray-100 min-h-screen font-sans antialiased">
        <nav className="border-b border-gray-800 px-6 py-3 flex items-center gap-4">
          <Link href="/" className="text-red-500 font-bold text-lg tracking-tight hover:text-red-400">
            SiteMedic
          </Link>
          <span className="text-gray-500 text-sm">Autonomous SRE</span>
          <div className="ml-auto flex items-center gap-4">
            <Link
              href="/analytics"
              className="text-sm text-gray-500 hover:text-gray-300"
            >
              Analytics
            </Link>
            <Link
              href="/compare"
              className="text-sm text-gray-500 hover:text-gray-300"
            >
              Compare
            </Link>
            <Link
              href="/audit"
              className="text-sm text-gray-500 hover:text-gray-300 flex items-center gap-1"
            >
              <span>🔒</span>
              <span>Audit</span>
            </Link>
            <Link
              href="/system-health"
              className="text-sm text-gray-500 hover:text-gray-300"
            >
              Health
            </Link>
            <Link
              href="/settings/cost"
              className="text-sm text-gray-500 hover:text-gray-300"
            >
              Settings
            </Link>
            <Link
              href="/demo"
              className="text-sm text-amber-500 hover:text-amber-300"
            >
              Demo
            </Link>
            <DemoStatusIndicator />
          </div>
        </nav>
        <DemoPublicBanner />
        <DemoWelcomeBanner />
        <main className="max-w-7xl mx-auto px-4 py-8">{children}</main>
      </body>
    </html>
  );
}
