/** 為每次 quick Playwright run 保留一個唯一 loopback port，避免共享工作樹並行驗收互撞。 */
import { spawn } from "node:child_process";
import { open, readFile, unlink } from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";

const host = "127.0.0.1";

function listenEphemeral() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.once("error", reject);
    server.listen(0, host, () => resolve(server));
  });
}

async function removeStaleLock(lockPath) {
  try {
    const owner = Number((await readFile(lockPath, "utf8")).trim());
    if (Number.isInteger(owner) && owner > 0) {
      try {
        process.kill(owner, 0);
        return false;
      } catch (error) {
        if (error?.code !== "ESRCH") return false;
      }
    }
    await unlink(lockPath);
    return true;
  } catch (error) {
    return error?.code === "ENOENT";
  }
}

async function reserveUniquePort() {
  for (;;) {
    const server = await listenEphemeral();
    const address = server.address();
    if (!address || typeof address === "string") {
      server.close();
      throw new Error("無法取得動態 loopback port");
    }
    const lockPath = path.join(os.tmpdir(), `loop-agent-lite-e2e-${address.port}.lock`);
    try {
      const handle = await open(lockPath, "wx");
      await handle.writeFile(String(process.pid));
      await handle.close();
      return { port: address.port, lockPath, server };
    } catch (error) {
      server.close();
      if (error?.code !== "EEXIST") throw error;
      await removeStaleLock(lockPath);
    }
  }
}

function closeServer(server) {
  return new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
}

const reservation = await reserveUniquePort();
let child;
try {
  // The lock file prevents another wrapper from choosing this port after the reservation socket closes.
  await closeServer(reservation.server);
  const executable = path.resolve("node_modules", ".bin", process.platform === "win32" ? "playwright.cmd" : "playwright");
  child = spawn(executable, ["test", ...process.argv.slice(2)], {
    stdio: "inherit",
    env: {
      ...process.env,
      LOOP_E2E_PORT: String(reservation.port),
      LOOP_E2E_URL: `http://${host}:${reservation.port}`,
    },
  });
  const forwardInterrupt = () => child?.kill("SIGINT");
  const forwardTerminate = () => child?.kill("SIGTERM");
  process.once("SIGINT", forwardInterrupt);
  process.once("SIGTERM", forwardTerminate);
  const exitCode = await new Promise((resolve, reject) => {
    child.once("error", reject);
    child.once("exit", (code, signal) => resolve(code ?? (signal ? 1 : 0)));
  });
  process.off("SIGINT", forwardInterrupt);
  process.off("SIGTERM", forwardTerminate);
  process.exitCode = exitCode;
} finally {
  await unlink(reservation.lockPath).catch(() => {});
}
