// Imported by railway.ts BEFORE "railway/iac" (MIP-2480).
//
// The SDK runs `$_ --version` to check the Railway CLI is new enough. The shell
// sets `$_` to whatever it launched, so under `timeout railway config plan` (or
// another wrapper) the SDK asks the wrapper for its version and fails with
// "requires Railway CLI 5.42.1 or newer". Dropping a `$_` that is not named like
// the CLI makes the SDK use `railway` from PATH. The name must match exactly: a
// script such as `railway-plan.sh` is a wrapper too.
//
// This only works from the repo root. Run inside `.railway/`, the CLI loads the
// SDK before this file.
const launched = (process.env._ ?? "").split(/[\\/]/).pop();
if (!["railway", "railway.exe", "railway.js"].includes(launched)) delete process.env._;
