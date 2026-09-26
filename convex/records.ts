import { v } from "convex/values";
import { paginationOptsValidator } from "convex/server";
import { internalMutation, internalQuery } from "./_generated/server";

const record = v.object({kind: v.string(), id: v.string(), value: v.string()});
const change = v.object({kind: v.string(), id: v.string(), value: v.union(v.string(), v.null())});
const stickyKinds = ["channel", "sticky", "slowSticky", "embedSticky", "embedImage",
  "bigEmbedImage", "webhookUrl", "webhookMessage"];
function check(kind: string) {
  if (!stickyKinds.includes(kind)) throw new Error("Only sticky-message data may be stored");
}

export const state = internalQuery({
  args: {paginationOpts: paginationOptsValidator},
  returns: v.object({page: v.array(record), isDone: v.boolean(), continueCursor: v.string()}),
  handler: async (ctx, args) => {
    const result = await ctx.db.query("records").paginate(args.paginationOpts);
    return {page: result.page.map(({kind, id, value}) => ({kind, id, value})),
      isDone: result.isDone, continueCursor: result.continueCursor};
  },
});

export const upsert = internalMutation({
  args: {kind: v.string(), id: v.string(), value: v.string()}, returns: v.null(),
  handler: async (ctx, args) => {
    check(args.kind);
    const existing = await ctx.db.query("records").withIndex("by_kind_id",
      q => q.eq("kind", args.kind).eq("id", args.id)).unique();
    const row = {...args, updatedAt: Date.now()};
    if (existing) await ctx.db.patch(existing._id, row);
    else await ctx.db.insert("records", row);
    return null;
  },
});

export const remove = internalMutation({
  args: {kind: v.string(), id: v.string()}, returns: v.null(),
  handler: async (ctx, args) => {
    const existing = await ctx.db.query("records").withIndex("by_kind_id",
      q => q.eq("kind", args.kind).eq("id", args.id)).unique();
    if (existing) await ctx.db.delete(existing._id);
    return null;
  },
});

export const removeChannel = internalMutation({
  args: {channelId: v.string()}, returns: v.null(),
  handler: async (ctx, args) => {
    for (const kind of stickyKinds) {
      const row = await ctx.db.query("records").withIndex("by_kind_id",
        q => q.eq("kind", kind).eq("id", args.channelId)).unique();
      if (row) await ctx.db.delete(row._id);
    }
    return null;
  },
});

export const batch = internalMutation({
  args: {changes: v.array(change)}, returns: v.null(),
  handler: async (ctx, {changes}) => {
    if (changes.length > 100) throw new Error("Batch limit is 100 records");
    for (const {kind, id, value} of changes) {
      if (value !== null) check(kind);
      const existing = await ctx.db.query("records").withIndex("by_kind_id",
        q => q.eq("kind", kind).eq("id", id)).unique();
      if (value === null) {
        if (existing) await ctx.db.delete(existing._id);
      } else {
        const row = {kind, id, value, updatedAt: Date.now()};
        if (existing) await ctx.db.patch(existing._id, row);
        else await ctx.db.insert("records", row);
      }
    }
    return null;
  },
});
