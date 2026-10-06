import { cpSync } from "node:fs";
cpSync(".next/static", ".next/standalone/.next/static", { recursive: true });
process.env.HOSTNAME = "0.0.0.0";
await import("./.next/standalone/server.js");
