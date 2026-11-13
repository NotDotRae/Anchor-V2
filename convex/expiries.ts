import { v } from "convex/values";
import { paginationOptsValidator } from "convex/server";
import { internalMutation, internalQuery } from "./_generated/server";
import type { MutationCtx } from "./_generated/server";
import { internal } from "./_generated/api";

// Called inside the same transaction that saves a channel, including outbox retries.
export async function syncExpiry(ctx: MutationCtx, channelId: string, value: string | null) {
  const previous = await ctx.db.query("pinExpiries").withIndex("by_channel",
    q => q.eq("channelId", channelId)).unique();
  const state = value === null ? null : JSON.parse(value);
  const expiry = state?.expiry;
  const active = state?.pins && Object.keys(state.pins).length > 0;
  if (expiry && active && (state.id !== channelId || typeof state.guild_id !== "string"
      || typeof expiry.token !== "string" || !expiry.token
      || !Number.isFinite(expiry.expires_at) || expiry.expires_at <= 0)) {
    throw new Error("Invalid pin expiry");
  }
  if (previous && expiry && active && previous.token === expiry.token
      && previous.guildId === state.guild_id && previous.expiresAt === expiry.expires_at * 1000) return;
  if (previous) {
    if (previous.status === "scheduled") await ctx.scheduler.cancel(previous.scheduledId);
    await ctx.db.delete(previous._id);
  }
  if (!expiry || !active) return;
  const expiresAt = expiry.expires_at * 1000;
  const scheduledId = await ctx.scheduler.runAt(expiresAt, internal.expiries.fire,
    {channelId, token: expiry.token});
  await ctx.db.insert("pinExpiries", {channelId, guildId: state.guild_id,
    token: expiry.token, expiresAt, scheduledId, status: "scheduled"});
}

export const fire = internalMutation({
  args: {channelId: v.string(), token: v.string()}, returns: v.null(),
  handler: async (ctx, {channelId, token}) => {
    const job = await ctx.db.query("pinExpiries").withIndex("by_channel",
      q => q.eq("channelId", channelId)).unique();
    // A canceled or replaced pin must never be expired by an old job.
    if (job && job.token === token && job.expiresAt <= Date.now()) {
      await ctx.db.patch(job._id, {status: "ready"});
    }
    return null;
  },
});

export const ready = internalQuery({
  args: {paginationOpts: paginationOptsValidator},
  returns: v.object({page: v.array(v.object({channel: v.string(), guild: v.string(), token: v.string()})),
    isDone: v.boolean(), continueCursor: v.string()}),
  handler: async (ctx, args) => {
    const result = await ctx.db.query("pinExpiries").withIndex("by_status",
      q => q.eq("status", "ready")).paginate(args.paginationOpts);
    return {page: result.page.map(job => ({channel: job.channelId, guild: job.guildId, token: job.token})),
      isDone: result.isDone, continueCursor: result.continueCursor};
  },
});
