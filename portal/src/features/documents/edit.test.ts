import { describe, expect, it } from "vitest";
import type { Field } from "@/api/contract";
import { changes, parse, toDraft } from "./edit";

describe("an amount, as typed", () => {
  it.each([
    ["1234.5", "1234.50"],
    ["1234", "1234.00"],
    ["12,5", "12.50"],
    ["1,234.56", "1234.56"],
    ["1.234,56", "1234.56"],
    ["1,234,567", "1234567.00"],
    ["1 234,56", "1234.56"],
    ["1\u00a0234,56", "1234.56"],
    ["-3.10", "-3.10"],
  ])("reads %s as %s", (typed, wire) => {
    expect(parse("money", typed)).toEqual({ value: wire });
  });

  it.each(["12.345", "1,234", "1,23,4", "12.3.4", "£12", "twelve"])(
    "refuses %s rather than guess",
    (typed) => {
      expect(parse("money", typed)).toHaveProperty("error");
    },
  );
});

describe("other values", () => {
  it("leaves an optional field empty, and refuses to for a required one", () => {
    expect(parse("string", "  ")).toEqual({ value: null });
    expect(parse("string", "", true)).toEqual({ error: "Required" });
  });

  it("wants a whole number where the field says integer", () => {
    expect(parse("number", "2,5")).toEqual({ value: 2.5 });
    expect(parse("integer", "2.5")).toEqual({ error: "Enter a whole number" });
  });
});

const fields: Field[] = [
  {
    name: "total",
    label: "Total",
    type: "money",
    value: "91.19",
    required: true,
    derived: false,
    problem: null,
    corrected: null,
    location: null,
  },
  {
    name: "line_items",
    label: "Line items",
    type: "list",
    required: false,
    derived: false,
    problem: null,
    corrected: null,
    columns: [
      { name: "name", label: "Name", type: "string" },
      { name: "amount", label: "Amount", type: "money" },
    ],
    rows: [{ name: "Tea", amount: "3.20" }],
    row_locations: [null],
  },
];

describe("what a draft changes", () => {
  it("is nothing, when nothing was changed", () => {
    expect(changes(fields, toDraft(fields))).toEqual({ values: {}, errors: {} });
  });

  it("is nothing, when a value was retyped the same in another style", () => {
    const draft = { ...toDraft(fields), total: "91,19" };
    expect(changes(fields, draft).values).toEqual({});
  });

  it("sends a changed list whole, and names the cell that is wrong", () => {
    const draft = {
      ...toDraft(fields),
      line_items: [
        { name: "Tea", amount: "3.20" },
        { name: "Cake", amount: "x" },
      ],
    };
    expect(changes(fields, draft)).toEqual({
      values: { line_items: [{ name: "Tea", amount: "3.20" }, { name: "Cake" }] },
      errors: { "line_items.1.amount": "Enter an amount, like 1234.56" },
    });
  });
});
