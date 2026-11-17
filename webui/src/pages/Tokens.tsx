import TokensPage from "../features/TokensPage";

export default function Tokens({ onManageWorkspaces }: { onManageWorkspaces: () => void }) {
  return (
    <TokensPage
      onManageWorkspaces={onManageWorkspaces}
      clientName="database"
      exampleUrl="https://db.example.com/mcp"
      descriptionPlaceholder="例如：数据库查询与排障"
    />
  );
}
