import Link from 'next/link';
import { appName, tagline } from '@/lib/shared';

export default function HomePage() {
  return (
    <main className="flex flex-1 flex-col items-center justify-center px-4 py-20">
      <div className="w-full max-w-2xl">
        <p className="mb-3 text-sm font-medium tracking-wide text-fd-muted-foreground">
          axstream-spec 0.1
        </p>
        <h1 className="text-4xl font-bold tracking-tight sm:text-5xl">
          {appName}
        </h1>
        <p className="mt-4 text-lg text-fd-muted-foreground">{tagline}</p>
        <p className="mt-4 text-fd-muted-foreground">
          An LLM streams actions one JSON object per line; the executor performs
          each action the moment its newline arrives — while the model is still
          generating. The newline is the commit signal, so a half-generated
          action can never fire, and execution overlaps generation instead of
          waiting for the full response.
        </p>

        <pre className="mt-8 overflow-x-auto rounded-lg border bg-fd-secondary/50 p-4 text-sm leading-relaxed">
          <code>{`{"op":"act","do":"open","target":"Notes"}
{"op":"act","do":"wait","ms":500}
{"op":"act","do":"type","text":"remember to buy milk"}
{"op":"done","status":"success"}`}</code>
        </pre>

        <div className="mt-8 flex flex-wrap gap-3">
          <Link
            href="/docs"
            className="rounded-lg bg-fd-primary px-5 py-2.5 text-sm font-medium text-fd-primary-foreground transition-opacity hover:opacity-90"
          >
            Read the docs
          </Link>
          <Link
            href="/docs/spec"
            className="rounded-lg border px-5 py-2.5 text-sm font-medium transition-colors hover:bg-fd-accent"
          >
            The spec
          </Link>
        </div>
      </div>
    </main>
  );
}
