import type { DashboardItem } from "@/src/web/web_queries.ts";

interface DashboardDataViewProps {
  readonly title: string;
  readonly eyebrow: string;
  readonly description: string;
  readonly items?: readonly DashboardItem[];
  readonly metrics?: DashboardItem;
  readonly query?: string;
}

const display = (value: unknown): string => {
  if (value === null || value === undefined) return "-";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
};

export function DashboardDataView(
  { title, eyebrow, description, items = [], metrics, query = "" }:
    DashboardDataViewProps,
) {
  const rows = metrics ? Object.entries(metrics) : [];
  const columns = items.length ? Object.keys(items[0]) : [];
  return (
    <div class="app-shell">
      <header class="site-header">
        <a class="brand" href="/dashboard">QBot4K</a>
        <nav aria-label="Dashboard navigation">
          <a href="/dashboard">Overview</a>
          <a href="/users">Users</a>
          <a href="/search">Search</a>
          <a href="/signals">Signals</a>
          <a href="/analytics">Analytics</a>
        </nav>
      </header>
      <main class="page-content">
        <section class="data-heading">
          <div>
            <p class="eyebrow">{eyebrow}</p>
            <h1>{title}</h1>
            <p class="lede">{description}</p>
          </div>
          {query !== undefined && title !== "Overview"
            ? (
              <form method="get" action={`/${title.toLocaleLowerCase()}`}>
                <label for="q">
                  Filter<input id="q" name="q" value={query} />
                </label>
                <button type="submit">Apply</button>
              </form>
            )
            : null}
        </section>
        {metrics
          ? (
            <dl class="metric-grid">
              {rows.map(([name, value]) => (
                <div key={name}>
                  <dt>{name.replaceAll("_", " ")}</dt>
                  <dd>{display(value)}</dd>
                </div>
              ))}
            </dl>
          )
          : (
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>
                    {columns.map((column) => (
                      <th key={column} scope="col">
                        {column.replaceAll("_", " ")}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {items.map((item, rowIndex) => (
                    <tr key={String(item.id ?? item.user_id ?? rowIndex)}>
                      {columns.map((column) => (
                        <td key={column}>{display(item[column])}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
              {!items.length ? <p class="empty-state">No results.</p> : null}
            </div>
          )}
      </main>
    </div>
  );
}
