/**
 * 版本单源守卫（E线）
 * - 背景：PR-181 只收口了 Python 侧（pyproject/__init__/Dockerfile），前端 package.json 仍是
 *   0.2.0、App.tsx 还写死 "0.2.0 · demo-ready"，与后端 0.3.0 漂移。
 * - 锁定三条：① 展示版本取自 package.json；② 组件不再出现字面版本号；③ 前端版本 == pyproject 版本。
 */
import { readFileSync } from "node:fs"
import { describe, expect, it } from "vitest"
import { APP_VERSION } from "../config/version"
import pkg from "../../package.json"

const read = (rel: string) => readFileSync(new URL(rel, import.meta.url), "utf8")

describe("版本单源", () => {
  it("展示版本取自 package.json，不在组件里写死", () => {
    expect(APP_VERSION).toBe(pkg.version)
    expect(read("../App.tsx")).not.toMatch(/\d+\.\d+\.\d+/)
  })

  it("前端版本与后端 pyproject 版本一致", () => {
    const m = read("../../../pyproject.toml").match(/^version\s*=\s*"([^"]+)"/m)
    expect(m, "pyproject.toml 未解析到 version").not.toBeNull()
    expect(m?.[1]).toBe(APP_VERSION)
  })
})
