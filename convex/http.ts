import { httpRouter } from "convex/server";
import { httpAction } from "./_generated/server";
import { internal } from "./_generated/api";

const http = httpRouter();

function authorized(request: Request) {
  const expected = process.env.ANCHORBOT_CONVEX_KEY;
  const actual = request.headers.get("Authorization")?.replace(/^Bearer\s+/i, "");
  return Boolean(expected && actual && expected === actual);
}

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

http.route({
  path: "/state",
  method: "GET",
  handler: httpAction(async (ctx, request) => {
    if (!authorized(request)) {
      return json({ error: "unauthorized" }, 401);
    }
    const cursor = new URL(request.url).searchParams.get("cursor");
    return json(await ctx.runQuery(internal.records.state, {
      paginationOpts: {cursor, numItems: 100},
    }));
  }),
});

http.route({
  path: "/upsert",
  method: "POST",
  handler: httpAction(async (ctx, request) => {
    if (!authorized(request)) {
      return json({ error: "unauthorized" }, 401);
    }
    await ctx.runMutation(internal.records.upsert, await request.json());
    return json({ ok: true });
  }),
});

http.route({
  path: "/delete",
  method: "POST",
  handler: httpAction(async (ctx, request) => {
    if (!authorized(request)) {
      return json({ error: "unauthorized" }, 401);
    }
    await ctx.runMutation(internal.records.remove, await request.json());
    return json({ ok: true });
  }),
});

http.route({
  path: "/deleteChannel",
  method: "POST",
  handler: httpAction(async (ctx, request) => {
    if (!authorized(request)) {
      return json({ error: "unauthorized" }, 401);
    }
    await ctx.runMutation(internal.records.removeChannel, await request.json());
    return json({ ok: true });
  }),
});

http.route({
  path: "/batch", method: "POST",
  handler: httpAction(async (ctx, request) => {
    if (!authorized(request)) return json({error: "unauthorized"}, 401);
    await ctx.runMutation(internal.records.batch, await request.json());
    return json({ok: true});
  }),
});

export default http;
