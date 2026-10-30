/**
 * 最小 react 类型垫片：只声明 client bundle 实际用到的 react 成员。
 * react 本身由宿主 Web 外壳的静态模块表提供（tsdown 里列为 external），
 * 仓库不需要（也不应该）安装 @types/react。若将来装了 @types/react，删掉本文件即可。
 */
declare module 'react' {
  export type CSSProperties = Record<string, string | number | undefined>
  export type ReactElement = unknown
  export type ReactNode = unknown
  export function createElement(type: unknown, props?: unknown, ...children: unknown[]): ReactElement
  export function useCallback<T extends (...args: never[]) => unknown>(fn: T, deps: unknown[]): T
  export function useEffect(effect: () => void | (() => void), deps?: unknown[]): void
  export function useState<S>(initial: S | (() => S)): [S, (next: S | ((prev: S) => S)) => void]
}
