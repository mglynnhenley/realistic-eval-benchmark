/* Local Markdown rendering for recorded model text. No remote assets or HTML. */
(() => {
  "use strict";
  const md = window.markdownit({html: false, linkify: false, typographer: false});
  const validateLink = md.validateLink.bind(md);
  md.validateLink = (url) => validateLink(url) && /^(?:https?:\/\/|mailto:|#)/i.test(url);
  const linkOpen = md.renderer.rules.link_open || ((tokens, index, options, env, renderer) =>
    renderer.renderToken(tokens, index, options));
  md.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
    tokens[index].attrSet("target", "_blank");
    tokens[index].attrSet("rel", "noopener noreferrer");
    return linkOpen(tokens, index, options, env, renderer);
  };
  // Display image descriptions without fetching URLs from a model transcript.
  md.renderer.rules.image = (tokens, index, options, env, renderer) => {
    const alt = renderer.renderInlineAsText(tokens[index].children || [], options, env);
    return md.utils.escapeHtml(alt ? `[Image: ${alt}]` : "[Image]");
  };
  md.renderer.rules.table_open = () => '<div class="markdown-table"><table>\n';
  md.renderer.rules.table_close = () => '</table></div>\n';
  window.RefundMarkdown = Object.freeze({render: (text) => md.render(String(text))});
})();
