export default function WorkspaceHeading({ eyebrow, title, description }: {
  eyebrow: string; title: string; description: string;
}): React.ReactElement {
  return <div className="workspace-heading">
    <div><span className="eyebrow">{eyebrow}</span><h1>{title}</h1><p>{description}</p></div>
    <span className="workspace-badge">Wildfire planning demo</span>
  </div>;
}
