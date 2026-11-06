import type { ComponentType } from "react";
export interface FrontendPlugin {
  id: string;
  apiVersion: readonly [number, number];
  pages?: { path: string; title: string; component: ComponentType }[];
  events?: string[];
}
