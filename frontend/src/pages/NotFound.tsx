import { Link } from "react-router-dom";

import { Card, Empty, PageHeader } from "../components/ui";

export function NotFound() {
  return (
    <>
      <PageHeader title="Page not found" />
      <Card>
        <Empty>
          Nothing here. <Link className="text-accent underline" to="/">Back to the overview</Link>
        </Empty>
      </Card>
    </>
  );
}
