/* Markdown → 适合朗读的纯文本。
 *
 * 模型回复几乎都是 Markdown，直接丢给语音引擎会把 ** 、## 、- 这些符号
 * 一个个念出来（"星号星号"、"井号井号"）。这里把结构转成语音能自然
 * 表达的等价物：标题加停顿，列表读成"第一、第二"，代码块整段跳过。
 *
 * 只在朗读前调用，不改变界面上显示的内容。
 */
(function () {
  function stripMarkdown(src) {
    let text = String(src || '');

    // 代码块整段去掉：念出来毫无意义，还会打断节奏
    text = text.replace(/```[\s\S]*?```/g, '（代码略）');
    text = text.replace(/~~~[\s\S]*?~~~/g, '（代码略）');

    // 行内代码保留内容，去掉反引号
    text = text.replace(/`([^`]+)`/g, '$1');

    // 图片和链接：保留可读部分
    text = text.replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1');
    text = text.replace(/\[([^\]]+)\]\([^)]*\)/g, '$1');

    // 表格：整块换成一句话，逐格念没意义
    text = text.replace(/^\s*\|.*\|\s*$/gm, '');
    text = text.replace(/^\s*[-:| ]{3,}\s*$/gm, '');

    // 标题：去掉井号，后面补个停顿
    text = text.replace(/^\s{0,3}#{1,6}\s+(.*)$/gm, '$1。');

    // 引用：去掉 >
    text = text.replace(/^\s{0,3}>\s?/gm, '');

    // 分隔线：换成空行（保留段落间隔，避免"上面下面"黏在一起）
    text = text.replace(/^\s*([-*_])\1{2,}\s*$/gm, '');

    // 无序列表："- item" / "* item" / "+ item" → 直接去掉符号
    text = text.replace(/^\s{0,3}[-*+]\s+/gm, '');

    // 有序列表：保留数字，念出来是"第一"还是"1"取决于引擎，
    // 这里统一改成中文序数，中文语音读起来更自然
    const cn = ['一', '二', '三', '四', '五', '六', '七', '八', '九', '十'];
    text = text.replace(/^\s{0,3}(\d{1,2})[.)]\s+/gm, (_m, n) => {
      const i = parseInt(n, 10) - 1;
      return (cn[i] || n) + '、';
    });

    // 强调符号：**bold** / __bold__ / *em* / _em_ / ~~del~~
    //
    // 这里的分寸很微妙。既要清掉 Markdown 标记，又不能误伤：
    //   "2 * 3"        乘号
    //   "snake_case"   单词内的下划线
    //   "a*b"          数学写法
    // 判断依据：成对出现、紧贴文字（内侧无空格）、且不在单词中间。
    text = text.replace(/\*\*\*([^*]+)\*\*\*/g, '$1');
    text = text.replace(/\*\*([^*\s][^*]*?[^*\s]|[^*\s])\*\*/g, '$1');
    text = text.replace(/__([^_\s][^_]*?[^_\s]|[^_\s])__/g, '$1');
    text = text.replace(/~~([^~]+)~~/g, '$1');

    // 单个星号/下划线的斜体：要求它包住的是"中文或连续字母数字"，
    // 且两侧是空白或标点。这样 "2 * 3" 的星号两侧是空格+数字，不会被误判。
    // 前一个字符可以是中文（"和*斜体*紧贴"这种写法），但不能是数字末尾，
    // 否则 "2 * 3" 里的星号会被误当成斜体标记。
    text = text.replace(
      /(^|[\s(（\[【<>「『]|[\u4e00-\u9fa5])\*([\u4e00-\u9fa5A-Za-z][^*\n]{0,80}?)\*(?=[\s)）\]】<>」』。，！？；：、,.]|[\u4e00-\u9fa5]|$)/g,
      '$1$2');
    text = text.replace(
      /(^|[\s(（\[【<>「『])_([\u4e00-\u9fa5A-Za-z][^_\n]{0,80}?)_(?=[\s)）\]】<>」』。，！？；：、,.]|$)/g,
      '$1$2');

    // 兜底：只清"成对残留"和"行首标记"。
    // 不碰单独的 * 和 _——那可能是有意义的乘号或标识符。
    text = text.replace(/^\s*[>#]+\s*/gm, '');       // 行首的 > 和 #
    text = text.replace(/\*{2,}/g, '');              // 连续星号残留
    text = text.replace(/_{2,}/g, '');                // 连续下划线残留
    text = text.replace(/`/g, '');                    // 反引号

    // HTML 标签
    text = text.replace(/<[^>]+>/g, '');

    // 空白整理：连续空行压成一个停顿，行内多空格压成一个
    text = text.replace(/[ \t]+/g, ' ');
    text = text.replace(/\n{3,}/g, '\n\n');

    return text.trim();
  }

  window.MarkdownSpeech = { strip: stripMarkdown };
})();
