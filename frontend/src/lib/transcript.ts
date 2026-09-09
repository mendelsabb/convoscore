// Turns a pasted transcript into the structured message list the API expects.
//
// Support conversations get pasted in as plain text, so the submit form accepts that shape and
// does the conversion here rather than making a reviewer hand-write JSON. The API itself only
// ever accepts the structured form: this is a convenience at the edge, not a second input format.

import type { Message } from "../api/types";

const SPEAKER_PATTERN = /^\s*(customer|agent|user|support|client|rep)\s*:\s*(.*)$/i;

const ROLE_ALIASES: Record<string, "customer" | "agent"> = {
  customer: "customer",
  user: "customer",
  client: "customer",
  agent: "agent",
  support: "agent",
  rep: "agent",
};

export interface ParseResult {
  messages: Message[];
  error: string | null;
}

/**
 * Parse lines of the form "Customer: text". Consecutive lines from the same speaker are joined,
 * so a multi-line paste does not become a dozen one-line messages.
 */
export function parseTranscript(raw: string): ParseResult {
  const messages: Message[] = [];
  let current: Message | null = null;

  for (const line of raw.split("\n")) {
    const match = line.match(SPEAKER_PATTERN);

    if (match) {
      const role = ROLE_ALIASES[match[1].toLowerCase()];
      const content = match[2].trim();
      if (current) messages.push(current);
      current = { role, content };
      continue;
    }

    const continuation = line.trim();
    if (!continuation) continue;

    if (!current) {
      return {
        messages: [],
        error: 'Each line needs a speaker, for example: "Customer: my order is late".',
      };
    }
    current.content = current.content ? `${current.content} ${continuation}` : continuation;
  }

  if (current) messages.push(current);

  const nonEmpty = messages.filter((message) => message.content.length > 0);

  if (nonEmpty.length === 0) {
    return { messages: [], error: "Add at least one message." };
  }
  if (!nonEmpty.some((message) => message.role === "customer")) {
    return { messages: [], error: "A conversation needs at least one customer message." };
  }

  return { messages: nonEmpty, error: null };
}

export const EXAMPLE_TRANSCRIPT = `Customer: This is the third time I've contacted you about being double charged.
Agent: I'm sorry about that. Let me escalate this to our billing team.
Customer: You said that last week. If it isn't refunded today I'm cancelling.`;
