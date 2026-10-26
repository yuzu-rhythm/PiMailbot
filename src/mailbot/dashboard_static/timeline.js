'use strict';
/* One JST day. The calendar is read-only; all positioning uses real event times. */
const DayTimeline = (() => {
  const pixelsPerMinute = 1.4;
  const minimumHeight = 48;
  let renderedDay = null;
  let manualUntil = 0;
  const element = (tag, text='', cls='') => {
    const result=document.createElement(tag); result.textContent=text;
    if(cls)result.className=cls; return result;
  };
  const time = value => new Intl.DateTimeFormat('ja-JP',{
    timeZone:'Asia/Tokyo',hour:'2-digit',minute:'2-digit',hour12:false
  }).format(new Date(value));

  function arrange(events, midnight) {
    const result=events.map(event=>({event,
      start:Math.max(0,(new Date(event.start)-midnight)/60000),
      end:Math.min(1440,(new Date(event.end)-midnight)/60000)
    })).filter(row=>row.start<1440&&row.end>row.start);
    result.sort((a,b)=>a.start-b.start||b.end-a.end);
    let cluster=[],laneEnds=[],clusterEnd=-1;
    const finish=()=>{for(const row of cluster)row.columns=laneEnds.length;};
    for(const row of result){
      if(row.start>=clusterEnd){finish();cluster=[];laneEnds=[];clusterEnd=-1;}
      let lane=laneEnds.findIndex(end=>end<=row.start);
      if(lane<0)lane=laneEnds.length;
      row.lane=lane;
      // Include the minimum readable block height when allocating columns.
      const visualEnd=Math.max(row.end,row.start+minimumHeight/pixelsPerMinute);
      laneEnds[lane]=visualEnd;clusterEnd=Math.max(clusterEnd,visualEnd);cluster.push(row);
    }
    finish(); return result;
  }

  function updateClock(){
    const marker=document.getElementById('timeline-now');
    if(!marker)return;
    const now=new Date(),midnight=new Date(marker.dataset.day+'T00:00:00+09:00');
    const minutes=(now-midnight)/60000;
    marker.hidden=minutes<0||minutes>=1440;
    marker.style.top=(minutes*pixelsPerMinute)+'px';
    marker.querySelector('span').textContent=time(now);
    const scroller=marker.closest('.timeline-scroll');
    if(!marker.hidden&&Date.now()>manualUntil&&scroller)scroller.scrollTop=Math.max(0,(minutes-50)*pixelsPerMinute);
  }

  function render(root, options){
    const previous=root.querySelector('.timeline-scroll');
    const scrollTop=previous?previous.scrollTop:null;
    const oldDay=renderedDay;
    const notesOpen=Boolean(root.querySelector('.period-details')?.open);
    root.replaceChildren();
    const {rows,day,today,onDayChange}=options;
    const midnight=new Date(day+'T00:00:00+09:00');
    const offset=(midnight-new Date(today+'T00:00:00+09:00'))/86400000;
    const navigation=element('div','','day-navigation');
    const previousButton=element('button','‹','day-step');
    previousButton.type='button';previousButton.setAttribute('aria-label','前の日');previousButton.disabled=offset<=0;
    previousButton.onclick=()=>onDayChange(-1);
    const label=element('h3',new Intl.DateTimeFormat('ja-JP',{
      timeZone:'Asia/Tokyo',month:'long',day:'numeric',weekday:'short'
    }).format(midnight),'timeline-day');
    const nextButton=element('button','›','day-step');nextButton.type='button';
    nextButton.setAttribute('aria-label','次の日');nextButton.disabled=offset>=6;nextButton.onclick=()=>onDayChange(1);
    const todayButton=element('button','今日','today-button');todayButton.type='button';todayButton.disabled=offset===0;
    todayButton.onclick=()=>onDayChange(0);
    navigation.append(previousButton,label,nextButton,todayButton);root.append(navigation);
    if(rows===null){root.append(element('p','予定を取得しています。','timeline-message'));return;}
    const dayEnd=new Date(midnight.getTime()+86400000);
    const onDay=event=>event.all_day?
      event.start.slice(0,10)<=day&&day<event.end.slice(0,10):
      new Date(event.start)<dayEnd&&new Date(event.end)>midnight;
    const selected=rows.filter(onDay);
    const periods=selected.filter(event=>!event.all_day&&new Date(event.end)-new Date(event.start)>=86400000||
      event.all_day&&new Date(event.end)-new Date(event.start)>86400000);
    const allDay=selected.filter(event=>event.all_day&&!periods.includes(event));
    const timed=selected.filter(event=>!event.all_day&&!periods.includes(event));
    const notes=element('div','','timeline-notes');
    for(const event of allDay.slice(0,2)){
      const line=element('p','終日　'+event.title,'all-day-event');line.title=event.label+' · '+event.title;notes.append(line);
    }
    if(allDay.length>2)notes.append(element('p','ほか、終日の予定 '+(allDay.length-2)+'件','quiet'));
    if(periods.length){const details=element('details','','period-details');
      details.open=oldDay===day&&notesOpen;
      details.append(element('summary','期間中の予定 '+periods.length+'件'));
      for(const event of periods)details.append(element('p',event.title,'period-item'));
      notes.append(details);
    }
    if(notes.childNodes.length)root.append(notes);
    const scroller=element('div','','timeline-scroll');
    scroller.tabIndex=0;scroller.setAttribute('role','region');scroller.setAttribute('aria-label','0時から24時の予定。上下にスクロールできます');
    const pauseFollowing=()=>{manualUntil=Date.now()+5*60000;};
    for(const event of ['wheel','touchstart','pointerdown','keydown'])scroller.addEventListener(event,pauseFollowing,{passive:true});
    const board=element('div','','timeline-board');
    board.style.height=(1440*pixelsPerMinute+minimumHeight)+'px';
    for(let hour=0;hour<=24;hour++){
      const line=element('div','','hour-line');line.style.top=(hour*60*pixelsPerMinute)+'px';
      line.append(element('time',String(hour).padStart(2,'0')+':00','hour-label'));board.append(line);
    }
    const appointments=element('div','','timeline-appointments');
    const arranged=arrange(timed,midnight);
    for(const row of arranged){
      const event=row.event;
      const height=Math.max(minimumHeight,(row.end-row.start)*pixelsPerMinute-3);
      const block=element('article','','timeline-event'+(height<85?' compact':''));
      block.dataset.calendar=event.calendar;
      block.style.top=(row.start*pixelsPerMinute)+'px';block.style.height=height+'px';
      block.style.left=`calc(${row.lane/row.columns*100}% + 3px)`;
      block.style.width=`calc(${100/row.columns}% - 7px)`;
      const range=time(event.start)+'–'+time(event.end);
      block.setAttribute('aria-label',[range,event.title,event.label,event.location].filter(Boolean).join('、'));
      block.title=[range,event.title,event.label,event.location].filter(Boolean).join(' · ');
      block.append(element('p',range,'timeline-event-time'),element('h4',event.title,'timeline-event-title'));
      if(height>=125)block.append(element('p',[event.label,event.location].filter(Boolean).join(' · '),'timeline-event-source'));
      appointments.append(block);
    }
    board.append(appointments);
    const marker=element('div','','timeline-now');marker.id='timeline-now';marker.dataset.day=day;
    marker.append(element('span'));board.append(marker);scroller.append(board);root.append(scroller);
    const fitNotes=()=>{scroller.style.height=Math.max(260,440-Math.max(0,notes.offsetHeight-27))+'px';};
    fitNotes();notes.querySelector('details')?.addEventListener('toggle',fitNotes);
    root.append(element('p',timed.length?'時間の目盛りは上下にスクロールできます。':'この日の時間指定の予定はありません。','timeline-caption'));
    if(oldDay!==day)manualUntil=0;
    renderedDay=day;updateClock();
    // Retain manual scrolling on refresh, and show the current/first appointment on a new day.
    requestAnimationFrame(()=>{
      if(oldDay===day&&scrollTop!==null){scroller.scrollTop=scrollTop;updateClock();return;}
      const minute=day===today?(new Date()-midnight)/60000:
        arranged.length?arranged[0].start:8*60;
      scroller.scrollTop=Math.max(0,(minute-50)*pixelsPerMinute);
    });
  }
  return {render,updateClock};
})();
