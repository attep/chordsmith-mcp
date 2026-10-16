import { describe, expect, it } from "vitest";
import { durationClass, formatDuration } from "./format";

describe("formatDuration", () => {
  it("renders milliseconds under a second", () => {
    expect(formatDuration(0)).toBe("0 ms");
    expect(formatDuration(520.4)).toBe("520 ms");
    expect(formatDuration(999)).toBe("999 ms");
  });

  it("renders seconds with one decimal from a second up", () => {
    expect(formatDuration(1000)).toBe("1.0 s");
    expect(formatDuration(3523)).toBe("3.5 s");
    expect(formatDuration(5204)).toBe("5.2 s");
    expect(formatDuration(60_000)).toBe("60.0 s");
  });

  it("shows a dash when there is no duration", () => {
    expect(formatDuration(null)).toBe("–");
    expect(formatDuration(undefined)).toBe("–");
  });
});

describe("durationClass", () => {
  it("highlights slow calls", () => {
    expect(durationClass(1200)).toBe("");
    expect(durationClass(2999)).toBe("");
    expect(durationClass(3000)).toBe("duration-warn");
    expect(durationClass(9999)).toBe("duration-warn");
    expect(durationClass(10_000)).toBe("duration-bad");
    expect(durationClass(null)).toBe("");
  });
});
