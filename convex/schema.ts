import { defineSchema, defineTable } from "convex/server";
import { v } from "convex/values";

export default defineSchema({
  records: defineTable({
    kind: v.string(),
    id: v.string(),
    value: v.string(),
    updatedAt: v.number(),
  }).index("by_kind_id", ["kind", "id"]),
});
