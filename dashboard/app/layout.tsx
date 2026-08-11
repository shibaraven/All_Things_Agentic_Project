import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { headers } from "next/headers";
import type { ReactNode } from "react";
import "./globals.css";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export async function generateMetadata(): Promise<Metadata> {
  const requestHeaders = await headers();
  const host = requestHeaders.get("x-forwarded-host") ?? requestHeaders.get("host") ?? "localhost:3000";
  const protocol = requestHeaders.get("x-forwarded-proto") ?? (host.startsWith("localhost") || host.startsWith("127.0.0.1") ? "http" : "https");
  const origin = new URL(`${protocol}://${host}`);
  const socialImage = new URL("/og.png", origin).toString();
  return {
    metadataBase: origin,
    title: "ShiftZero — Autonomous Factory Operations",
    description: "A governed multi-agent control plane that plans, recovers and protects factory operations.",
    icons: { icon: "/favicon.svg", shortcut: "/favicon.svg" },
    openGraph: {
      title: "ShiftZero",
      description: "Autonomous factory operations that plan, recover and protect themselves.",
      images: [{ url: socialImage, width: 1731, height: 909, alt: "ShiftZero autonomous factory operations" }],
    },
    twitter: { card: "summary_large_image", title: "ShiftZero", description: "Autonomous factory operations", images: [socialImage] },
  };
}

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return <html lang="en"><body className={`${geistSans.variable} ${geistMono.variable}`}>{children}</body></html>;
}
