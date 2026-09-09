import { describe, expect, it } from "vitest";

import { parseTranscript } from "./transcript";
import { formatCost, formatDuration } from "./format";

describe("parseTranscript", () => {
  it("parses speaker-prefixed lines", () => {
    const { messages, error } = parseTranscript("Customer: hello\nAgent: hi there");
    expect(error).toBeNull();
    expect(messages).toEqual([
      { role: "customer", content: "hello" },
      { role: "agent", content: "hi there" },
    ]);
  });

  it("joins continuation lines onto the current message", () => {
    const { messages } = parseTranscript("Customer: my order\nis very late\nAgent: sorry");
    expect(messages[0]).toEqual({ role: "customer", content: "my order is very late" });
    expect(messages).toHaveLength(2);
  });

  it("accepts common speaker aliases", () => {
    const { messages } = parseTranscript("User: hi\nSupport: hello");
    expect(messages.map((m) => m.role)).toEqual(["customer", "agent"]);
  });

  it("ignores blank lines", () => {
    const { messages } = parseTranscript("Customer: one\n\n\nAgent: two\n");
    expect(messages).toHaveLength(2);
  });

  it("rejects text with no speaker", () => {
    const { error } = parseTranscript("just some text with no speaker");
    expect(error).toMatch(/speaker/i);
  });

  it("rejects an empty transcript", () => {
    expect(parseTranscript("   ").error).toMatch(/at least one message/i);
  });

  it("requires a customer message, matching the API rule", () => {
    expect(parseTranscript("Agent: anyone there?").error).toMatch(/customer/i);
  });
});

describe("formatting", () => {
  it("keeps sub-cent costs visible instead of rounding them to zero", () => {
    expect(formatCost("0.00035200")).toBe("$0.000352");
    expect(formatCost("0")).toBe("$0");
    expect(formatCost(null)).toBe("—");
  });

  it("formats durations in the unit that reads best", () => {
    expect(formatDuration(842)).toBe("842 ms");
    expect(formatDuration(5367)).toBe("5.37 s");
    expect(formatDuration(null)).toBe("—");
  });
});
