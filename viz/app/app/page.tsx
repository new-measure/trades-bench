"use client";

import dynamic from "next/dynamic";

// The viewer drives WebGL scenes and reads replay data at runtime, so it only renders in the browser.
const Viewer = dynamic(() => import("@/components/Viewer"), {
  ssr: false,
  loading: () => <div className="grid h-screen place-items-center text-muted">Loading run…</div>,
});

export default function Page() {
  return <Viewer />;
}
