import { defineSchema, defineTable } from "convex/server";
import { v } from "convex/values";

export default defineSchema({
  pinExpiries: defineTable({
    channelId: v.string(),
    guildId: v.string(),
    token: v.string(),
    expiresAt: v.number(),
    scheduledId: v.id("_scheduled_functions"),
    status: v.union(v.literal("scheduled"), v.literal("ready")),
  }).index("by_channel", ["channelId"]).index("by_status", ["status"]),
  records: defineTable({
    kind: v.string(),
    id: v.string(),
    value: v.string(),
    updatedAt: v.number(),
  }).index("by_kind_id", ["kind", "id"]),
});
