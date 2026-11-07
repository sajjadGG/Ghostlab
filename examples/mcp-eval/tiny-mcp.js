// Minimal stdio MCP server (no deps) used to verify OpenShell sandboxing.
const TOOLS = [
  {
    name: "echo",
    description: "Echo a message back to the caller.",
    inputSchema: {
      type: "object",
      properties: { message: { type: "string", description: "Text to echo." } },
      required: ["message"],
    },
    annotations: { readOnlyHint: true },
  },
  {
    name: "host_info",
    description: "Report the kernel the server is running on.",
    inputSchema: { type: "object", properties: {} },
    annotations: { readOnlyHint: true },
  },
];

function handle(msg) {
  if (msg.method === "initialize") {
    return {
      protocolVersion: "2025-06-18",
      serverInfo: { name: "tiny-mcp", version: "1.0.0" },
      capabilities: { tools: {} },
    };
  }
  if (msg.method === "tools/list") return { tools: TOOLS };
  if (msg.method === "tools/call") {
    const name = msg.params && msg.params.name;
    if (name === "echo") {
      const text = (msg.params.arguments || {}).message || "";
      return { content: [{ type: "text", text: `echo: ${text}` }] };
    }
    if (name === "host_info") {
      return {
        content: [
          { type: "text", text: `${process.platform} ${require("os").release()}` },
        ],
      };
    }
    return { isError: true, content: [{ type: "text", text: `unknown tool ${name}` }] };
  }
  return {};
}

let buffer = "";
process.stdin.on("data", (chunk) => {
  buffer += chunk;
  let index;
  while ((index = buffer.indexOf("\n")) !== -1) {
    const line = buffer.slice(0, index).trim();
    buffer = buffer.slice(index + 1);
    if (!line) continue;
    const msg = JSON.parse(line);
    if (msg.id === undefined) continue;
    process.stdout.write(
      JSON.stringify({ jsonrpc: "2.0", id: msg.id, result: handle(msg) }) + "\n"
    );
  }
});
