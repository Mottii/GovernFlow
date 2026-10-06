import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata = {
  title: "Governed — Automation control",
  description: "Spend, approvals and controls for your automated workflows",
};
export default function Layout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
