const clock = timeZone => new Intl.DateTimeFormat('en-GB',{
  hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23',...(timeZone?{timeZone}:{})
});

export function formatTerminal(entries,timeZone) {
  const time=clock(timeZone),lines=[];
  let line=[],cursor=0,stamp='';
  for(const entry of entries){
    for(const character of entry.text){
      if(character==='\r'){cursor=0;continue;}
      if(character==='\n'){
        lines.push((line.length?`[${stamp}] ${line.join('')}`:'')+'\n');line=[];cursor=0;stamp='';continue;
      }
      if(character==='\b'){cursor=Math.max(0,cursor-1);continue;}
      if(!line.length)stamp=time.format(new Date(entry.time));
      line[cursor++]=character;
    }
  }
  if(line.length)lines.push(`[${stamp}] ${line.join('')}`);
  return lines.join('');
}

export function formatTaskLogs(lines,startedAt,timeZone){
  const time=clock(timeZone),date=String(startedAt || '').slice(0,10);
  return lines.map(line=>{
    const current=line.match(/^\[([0-9]{4}-[^\]]+)\] (.*)$/s);
    const previous=line.match(/^\[(\d{2}:\d{2}:\d{2}) UTC\] (.*)$/s);
    if(current)return `[${time.format(new Date(current[1]))}] ${current[2]}`;
    if(previous && date)return `[${time.format(new Date(`${date}T${previous[1]}Z`))}] ${previous[2]}`;
    return line;
  }).join('\n');
}
